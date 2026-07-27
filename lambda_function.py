"""
Job Application Tracker Lambda function.

Runs once a day, triggered by EventBridge Scheduler. Fetches Gmail emails
since the last successful run, sends each one to Claude Haiku 4.5 on
Bedrock for classification and structured extraction, drops anything not
job related, and writes the rest to S3 partitioned by date.

Environment variables expected on the Lambda function:
    S3_BUCKET               e.g. job-application-tracker-data-shivam
    SSM_PARAMETER_NAME      e.g. /job-tracker/last-run-timestamp
    SECRETS_MANAGER_SECRET  e.g. job-tracker/gmail-oauth
    BEDROCK_MODEL_ID        e.g. anthropic.claude-haiku-4-5-20251001-v1:0
"""

import base64
import json
import logging
import os
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import boto3
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# --- Config from environment variables ---
S3_BUCKET = os.environ["S3_BUCKET"]
SSM_PARAMETER_NAME = os.environ["SSM_PARAMETER_NAME"]
SECRETS_MANAGER_SECRET = os.environ["SECRETS_MANAGER_SECRET"]
BEDROCK_MODEL_ID = os.environ["BEDROCK_MODEL_ID"]

# Fixed set of status categories, kept consistent so the dashboard can
# group and chart on this field reliably over time.
VALID_STATUSES = [
    "applied_confirmation",
    "interview_scheduled",
    "assessment_requested",
    "rejected",
    "offer",
    "not_job_related",
    "other",
]

EXTRACTION_PROMPT_TEMPLATE = """You are classifying and extracting structured data from a single email.

Return ONLY valid JSON matching this exact schema, no extra text, no markdown fences:
{{
  "company": string or null,
  "role": string or null,
  "status": one of {statuses},
  "date_received": string (ISO 8601, use the date_received value given below exactly),
  "sender_email": string,
  "summary": string (one short sentence),
  "requires_action": boolean
}}

Use this test to choose between "not_job_related" and "other":
  Ask: "Does this email involve me, as a job applicant, and a specific
  company or recruiter, in the context of a job search?"
  - If NO: the email is about something else entirely (a newsletter, a
    personal message, a receipt, a social notification, spam). Set status
    to "not_job_related".
  - If YES, but it does not describe one of the five specific stages listed
    above (applied_confirmation, interview_scheduled, assessment_requested,
    rejected, offer): Set status to "other".

Examples of "other": a recruiter messaging you cold about a role, a company
asking to reschedule without confirming a new time, a networking contact
replying about a referral, a salary or offer negotiation email, your own
withdrawal of an application.

Examples of "not_job_related": a newsletter or marketing email, a personal
email from a friend or family member, a receipt or shipping notification,
a social media alert, spam.

If you are unsure whether a job applicant and company/recruiter are both
involved, default to "not_job_related" rather than "other".

Email details:
Sender: {sender}
Subject: {subject}
Date received: {date_received}

Email body:
{body}
"""

# --- AWS clients ---
ssm_client = boto3.client("ssm")
secrets_client = boto3.client("secretsmanager")
s3_client = boto3.client("s3")
bedrock_client = boto3.client("bedrock-runtime")


def get_last_run_timestamp():
    """Reads the last run timestamp from SSM. Falls back to now if missing
    or invalid, which happens on the very first run."""
    try:
        response = ssm_client.get_parameter(Name=SSM_PARAMETER_NAME)
        value = response["Parameter"]["Value"]
        return datetime.fromisoformat(value)
    except (ssm_client.exceptions.ParameterNotFound, ValueError):
        return datetime.now(timezone.utc)


def update_last_run_timestamp(timestamp: datetime):
    ssm_client.put_parameter(
        Name=SSM_PARAMETER_NAME,
        Value=timestamp.isoformat(),
        Type="String",
        Overwrite=True,
    )


def get_gmail_service():
    """Builds an authenticated Gmail API client using the refresh token,
    client ID, and client secret stored in Secrets Manager."""
    secret_value = secrets_client.get_secret_value(SecretId=SECRETS_MANAGER_SECRET)
    secret = json.loads(secret_value["SecretString"])

    credentials = Credentials(
        token=None,
        refresh_token=secret["gmail_refresh_token"],
        client_id=secret["gmail_client_id"],
        client_secret=secret["gmail_client_secret"],
        token_uri="https://oauth2.googleapis.com/token",
    )
    return build("gmail", "v1", credentials=credentials)


def fetch_emails_since(gmail_service, since: datetime):
    """Fetches all emails received after the given timestamp."""
    query = f"after:{int(since.timestamp())}"
    emails = []

    request = gmail_service.users().messages().list(userId="me", q=query)
    while request is not None:
        response = request.execute()
        for msg_meta in response.get("messages", []):
            full_msg = (
                gmail_service.users()
                .messages()
                .get(userId="me", id=msg_meta["id"], format="full")
                .execute()
            )
            emails.append(parse_email(full_msg))
        request = gmail_service.users().messages().list_next(request, response)

    return emails


def parse_email(full_msg):
    """Extracts sender, subject, date, and plain text body from a Gmail
    API message resource."""
    headers = {h["name"]: h["value"] for h in full_msg["payload"]["headers"]}
    sender = headers.get("From", "")
    subject = headers.get("Subject", "")
    date_header = headers.get("Date", "")

    try:
        date_received = parsedate_to_datetime(date_header).isoformat()
    except (TypeError, ValueError):
        date_received = datetime.now(timezone.utc).isoformat()

    body = extract_body_text(full_msg["payload"])

    return {
        "sender": sender,
        "subject": subject,
        "date_received": date_received,
        "body": body,
    }


def extract_body_text(payload):
    """Recursively walks the Gmail payload structure to find the plain
    text part of the email body."""
    if payload.get("mimeType") == "text/plain" and "data" in payload.get("body", {}):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode(
            "utf-8", errors="replace"
        )

    for part in payload.get("parts", []):
        text = extract_body_text(part)
        if text:
            return text

    return ""


def classify_and_extract(email: dict) -> dict:
    """Sends one email to Claude Haiku 4.5 on Bedrock and returns the
    parsed structured JSON response."""
    prompt = EXTRACTION_PROMPT_TEMPLATE.format(
        statuses=VALID_STATUSES,
        sender=email["sender"],
        subject=email["subject"],
        date_received=email["date_received"],
        # Truncate very long bodies to keep token usage low and cheap.
        body=email["body"][:4000],
    )

    request_body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 500,
        "messages": [{"role": "user", "content": prompt}],
    }

    response = bedrock_client.invoke_model(
        modelId=BEDROCK_MODEL_ID,
        body=json.dumps(request_body),
    )
    response_body = json.loads(response["body"].read())
    model_text = response_body["content"][0]["text"]

    try:
        return json.loads(model_text)
    except json.JSONDecodeError:
        # If the model ever returns something malformed, treat it as
        # not job related rather than letting the whole run fail.
        return {
            "company": None,
            "role": None,
            "status": "not_job_related",
            "date_received": email["date_received"],
            "sender_email": email["sender"],
            "summary": "Could not parse model response.",
            "requires_action": False,
        }


def write_to_s3(records: list, run_date: datetime):
    key = (
        f"job-data/{run_date.year:04d}/{run_date.month:02d}/"
        f"{run_date.day:02d}.json"
    )
    s3_client.put_object(
        Bucket=S3_BUCKET,
        Key=key,
        Body=json.dumps(records, indent=2),
        ContentType="application/json",
    )
    return key


def handler(event, context):
    run_start = datetime.now(timezone.utc)

    last_run = get_last_run_timestamp()
    gmail_service = get_gmail_service()
    emails = fetch_emails_since(gmail_service, last_run)

    records = []
    for email in emails:
        result = classify_and_extract(email)
        logger.info(
            "DEBUG_CLASSIFICATION subject=%r sender=%r status=%r company=%r",
            email["subject"],
            email["sender"],
            result.get("status"),
            result.get("company"),
        )
        if result.get("status") != "not_job_related":
            records.append(result)

    s3_key = None
    if records:
        s3_key = write_to_s3(records, run_start)

    # Only update the timestamp after everything above succeeds, so a
    # failed run gets safely retried from the same starting point.
    update_last_run_timestamp(run_start)

    return {
        "emails_fetched": len(emails),
        "job_related_records": len(records),
        "s3_key": s3_key,
    }