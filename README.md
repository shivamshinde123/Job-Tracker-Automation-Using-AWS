# Job Application Tracker

A fully serverless pipeline that reads job application emails from Gmail every night, classifies and extracts structured data from them using an LLM on AWS Bedrock, stores the results in S3, and feeds a QuickSight dashboard, all without a server running anywhere.

**What it does:** every night at 11:59pm (America/New_York), a Lambda function wakes up, fetches every email received since its last run (inbox and sent), sends each one to Claude Haiku 4.5 on Bedrock to classify it and pull out structured fields (company, role, status, whether it needs a reply), writes the results to S3 as one JSON file per day, and goes back to sleep. QuickSight refreshes automatically two minutes later and the dashboard reflects last night's activity by morning.

---

## Table of contents

1. [Architecture](#architecture)
2. [Prerequisites](#prerequisites)
3. [Step 1 — Gmail OAuth](#step-1--gmail-oauth)
4. [Step 2 — AWS Secrets Manager](#step-2--aws-secrets-manager)
5. [Step 3 — S3 bucket](#step-3--s3-bucket)
6. [Step 4 — SSM Parameter Store](#step-4--ssm-parameter-store)
7. [Step 5 — IAM role](#step-5--iam-role)
8. [Step 6 — Bedrock model access](#step-6--bedrock-model-access)
9. [Step 7 — Lambda function](#step-7--lambda-function)
10. [Step 8 — EventBridge Scheduler](#step-8--eventbridge-scheduler)
11. [Step 9 — QuickSight dataset](#step-9--quicksight-dataset)
12. [Step 10 — Dashboard visuals](#step-10--dashboard-visuals)
13. [Classification logic explained](#classification-logic-explained)
14. [Testing and backfilling](#testing-and-backfilling)
15. [Bugs we hit and how they were fixed](#bugs-we-hit-and-how-they-were-fixed)
16. [Security notes](#security-notes)
17. [Repo structure](#repo-structure)

---

## Architecture

```
EventBridge Scheduler (cron, 11:59pm America/New_York)
        │
        ▼
Lambda (job-tracker-daily-run)
        │
        ├──> SSM Parameter Store   (read last-run timestamp)
        ├──> Secrets Manager       (read Gmail OAuth credentials)
        ├──> Gmail API             (fetch inbox + sent since last run)
        ├──> Amazon Bedrock        (Claude Haiku 4.5 — classify + extract)
        ├──> Amazon S3             (write deduped daily JSON, job-data/YYYY/MM/DD.json)
        └──> SSM Parameter Store   (update last-run timestamp, only on success)
        
Amazon S3 (job-data/ prefix)
        │
        ▼
Manifest file (points QuickSight at the prefix)
        │
        ▼
QuickSight SPICE dataset (scheduled refresh, 12:01am America/New_York)
        │
        ▼
Dashboard (5 visuals + day/month slicer)
```

No server runs continuously. Every piece is either a managed AWS service or a function that wakes up, runs for seconds to a couple of minutes, and goes back to sleep.

---

## Prerequisites

- An AWS account with billing enabled (this uses Lambda, S3, SSM, Secrets Manager, Bedrock, EventBridge Scheduler, and QuickSight — all pay-as-you-go, and at low personal-use volume this costs well under a few dollars a month)
- A Google account with Gmail, and access to Google Cloud Console
- Python 3.12 installed locally, with `pip` and the ability to create virtual environments
- A code editor (VS Code or similar)

---

## Step 1 — Gmail OAuth

We need a way for Lambda to read your Gmail inbox without you logging in every time. OAuth's refresh token gives us that.

1. Go to [Google Cloud Console](https://console.cloud.google.com), create a new project (e.g. `job-tracker-project`).
2. Under **APIs & Services → Library**, enable the **Gmail API**.
3. Under **APIs & Services → OAuth consent screen**:
   - Set publishing status to **Testing** (fine for personal use, avoids Google's full verification process).
   - Under **Test users**, add your own Gmail address. **This step is easy to miss and causes an `Error 403: access_denied` screen even when you're the project owner**, if you hit that error, this is almost always why.
   - Under **Scopes**, make sure `gmail.readonly` is listed.
4. Under **APIs & Services → Credentials**, click **Create Credentials → OAuth client ID**, choose **Desktop app** as the type (this avoids needing a verified redirect URI). Download the resulting `client_secret.json`.
5. Locally, install the OAuth library:
   ```
   pip install google-auth-oauthlib google-auth-httplib2 --break-system-packages
   ```
6. Place `client_secret.json` in your project folder, then run this one-time script (see `get_gmail_refresh_token.py` in this repo) to complete the OAuth flow:
   ```python
   from google_auth_oauthlib.flow import InstalledAppFlow

   SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
   CLIENT_SECRET_FILE = "client_secret.json"

   def main():
       flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
       credentials = flow.run_local_server(port=0)
       print(f"gmail_client_id: {credentials.client_id}")
       print(f"gmail_client_secret: {credentials.client_secret}")
       print(f"gmail_refresh_token: {credentials.refresh_token}")

   if __name__ == "__main__":
       main()
   ```
7. Run it:
   ```
   python get_gmail_refresh_token.py
   ```
   A browser window opens, log in with the Gmail account you want tracked, and approve read-only access. Your terminal prints three values: `gmail_client_id`, `gmail_client_secret`, and `gmail_refresh_token`. **Save these somewhere, you'll need all three in Step 2.**

   > If no refresh token is printed, you've likely run this flow before and Google is only issuing a refresh token on first consent. Revoke the app's access at [myaccount.google.com/permissions](https://myaccount.google.com/permissions) and run the script again.

---

## Step 2 — AWS Secrets Manager

1. In the AWS Console, go to **Secrets Manager → Store a new secret**.
2. Choose **Other type of secret**, and enter three key/value pairs:
   - `gmail_client_id`
   - `gmail_client_secret`
   - `gmail_refresh_token`
   (using the three values from Step 1)
3. Name the secret `job-tracker/gmail-oauth`.
4. Leave rotation off. Store it.
5. **Copy the full secret ARN** from the secret's detail page (it includes a random 6-character suffix, e.g. `...gmail-oauth-AbCdEf`), you'll need the *exact* ARN in Step 5's IAM policy. Using just the secret name instead of the full ARN is a common mistake that causes an `AccessDeniedException` later.

---

## Step 3 — S3 bucket

1. Go to **S3 → Create bucket**.
2. Give it a globally unique name (e.g. `job-application-tracker-data-yourname`).
3. Region: `us-east-1` (recommended — this is also the region with the widest Bedrock model availability).
4. Leave **Block all public access** on. Default encryption is fine. No need to enable versioning.
5. Decide on the key prefix pattern now: **`job-data/{year}/{month}/{day}.json`** — for example `job-data/2026/07/26.json`. You don't need to create any folders manually, Lambda creates them on write. Both the Lambda code and the QuickSight manifest (Step 9) need to agree on this exact pattern.

---

## Step 4 — SSM Parameter Store

Lambda has no memory between invocations, so it needs somewhere to store "when did I last run."

1. Go to **Systems Manager → Parameter Store → Create parameter**.
2. Name: `/job-tracker/last-run-timestamp` (the leading slash matters, SSM uses it as a hierarchy separator).
3. Tier: **Standard**. Type: **String**.
4. Value: `none` (a placeholder — the Lambda code checks for a value that fails to parse as a timestamp and treats that as "first run," falling back to the current time rather than trying to backfill everything).

---

## Step 5 — IAM role

Create an execution role for Lambda scoped narrowly to exactly what it needs, nothing more.

1. Go to **IAM → Roles → Create role**, trusted entity **AWS service**, use case **Lambda**.
2. Attach the AWS managed policy **AWSLambdaBasicExecutionRole** (covers CloudWatch logging).
3. Name the role `job-tracker-lambda-role`.
4. Add a custom inline policy named `job-tracker-lambda-permissions` with this JSON (see `job-tracker-lambda-permissions.json` in this repo — replace `YOUR_ACCOUNT_ID`, your bucket name, and the exact Secrets Manager ARN suffix):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "InvokeBedrockModel",
      "Effect": "Allow",
      "Action": "bedrock:InvokeModel",
      "Resource": [
        "arn:aws:bedrock:us-east-1:YOUR_ACCOUNT_ID:inference-profile/us.anthropic.claude-haiku-4-5-20251001-v1:0",
        "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0",
        "arn:aws:bedrock:us-east-2::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0",
        "arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0"
      ]
    },
    {
      "Sid": "BedrockMarketplaceAccess",
      "Effect": "Allow",
      "Action": ["aws-marketplace:ViewSubscriptions", "aws-marketplace:Subscribe"],
      "Resource": "*"
    },
    {
      "Sid": "ReadWriteJobDataInS3",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject"],
      "Resource": "arn:aws:s3:::YOUR_BUCKET_NAME/job-data/*"
    },
    {
      "Sid": "ListJobDataPrefix",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::YOUR_BUCKET_NAME",
      "Condition": { "StringLike": { "s3:prefix": "job-data/*" } }
    },
    {
      "Sid": "ReadWriteLastRunTimestamp",
      "Effect": "Allow",
      "Action": ["ssm:GetParameter", "ssm:PutParameter"],
      "Resource": "arn:aws:ssm:us-east-1:YOUR_ACCOUNT_ID:parameter/job-tracker/last-run-timestamp"
    },
    {
      "Sid": "ReadGmailOAuthSecret",
      "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "arn:aws:secretsmanager:us-east-1:YOUR_ACCOUNT_ID:secret:job-tracker/gmail-oauth-XXXXXX"
    }
  ]
}
```

A few of these need explaining, they aren't obvious the first time:

- **Both the inference profile ARN and the foundation-model ARNs are needed for Bedrock.** Claude Haiku 4.5 only works through a *cross-region inference profile* on Bedrock, not the bare model ID, but IAM still checks permission against whichever regional foundation model actually ends up handling the request, so both need to be listed.
- **The Marketplace permission is required once**, the first time your account invokes any Anthropic model on Bedrock, AWS auto-provisions a Marketplace subscription behind the scenes, and the Lambda role needs permission to accept that. It can't be scoped to a specific resource, hence the wildcard.
- **`s3:ListBucket` is needed even though it looks unrelated to reading/writing objects.** Without it, a `GetObject` call on a not-yet-existing key returns a misleading `AccessDenied` mentioning `ListBucket`, instead of a clean 404, this is an S3 quirk, not a bug in your code.

---

## Step 6 — Bedrock model access

1. Go to **Bedrock → Model access**, request access to **Claude Haiku 4.5** (the cheapest actively-supported, non-Legacy Anthropic model on Bedrock, well suited to structured extraction tasks like this one).
2. The first time any account invokes an Anthropic model, Bedrock requires a one-time use-case form (company name and URL). For a personal project, your own name and a GitHub/portfolio URL is fine here.
3. Once granted, note the exact model ID. For the cross-region inference profile, the ID to use in Lambda's environment variable is:
   ```
   us.anthropic.claude-haiku-4-5-20251001-v1:0
   ```
   (note the `us.` prefix — using the bare foundation-model ID without it causes a `ValidationException`.)

---

## Step 7 — Lambda function

The full code lives in `lambda_function.py` in this repo. Here's what each piece does, in the order it runs:

1. **`get_last_run_timestamp()`** — reads `/job-tracker/last-run-timestamp` from SSM. Falls back to the current time if missing or unparseable (first run).
2. **`get_gmail_service()`** — pulls the three OAuth values from Secrets Manager, builds an authenticated Gmail API client.
3. **`fetch_emails_since()`** — fetches the account's own email address via Gmail's `getProfile` call (never hardcoded), then fetches every email (inbox *and* sent) since the last-run timestamp, tagging each one with whether it was sent by the account owner or received from someone else.
4. **`parse_email()`** — extracts sender, subject, date, plain-text body, and the Gmail message ID (used later for deduplication) from each raw message.
5. **`classify_and_extract()`** — sends each email to Bedrock with a detailed prompt (see [Classification logic explained](#classification-logic-explained)) and parses the structured JSON response, stripping markdown code fences if present (see Bugs section — Claude sometimes wraps JSON in ` ```json ` fences despite instructions not to).
6. **`write_to_s3()`** — groups the results by each record's *own* `date_received` (not the run date), merges with anything already written for that day, deduplicates by `gmail_message_id`, and writes one JSON file per day.
7. **`update_last_run_timestamp()`** — only called after everything above succeeds, so a failed run safely retries the same window next time instead of silently skipping emails.

### Deploying it

**Dependencies** (`google-auth`, `google-auth-oauthlib`, `google-api-python-client`) aren't part of Lambda's default Python runtime, so they're packaged as a Lambda layer:

```
python3 -m venv venv
source venv/bin/activate          # venv\Scripts\activate on Windows
pip install -r requirements.txt -t python --platform manylinux2014_x86_64 --only-binary=:all: --python-version 3.12
```

The `--platform manylinux2014_x86_64 --only-binary=:all:` flags are essential if you're building on Windows or Mac, without them, pip installs a version of the compiled `cryptography` dependency that won't load on Lambda's Linux runtime (see Bugs section).

Zip it with Python's own zip module rather than a GUI tool or PowerShell's `Compress-Archive` (which was found to silently corrupt the compiled binary during zipping):
```
python -c "import shutil; shutil.make_archive('job-tracker-gmail-deps', 'zip', '.', 'python')"
```

**Uploading the layer:**
1. Since the zip likely exceeds the console's direct-upload size limit, upload it to any S3 bucket first, then in **Lambda → Layers → Create layer**, choose "Upload a file from Amazon S3" and paste the S3 URI. Set compatible runtime to Python 3.12.

**Creating the function:**
1. **Lambda → Functions → Create function → Author from scratch.**
2. Name: `job-tracker-daily-run`. Runtime: **Python 3.12**. **Architecture: x86_64** (must match whatever the layer was built for).
3. Permissions: use the existing role `job-tracker-lambda-role` from Step 5.
4. Paste `lambda_function.py`'s contents into the code editor, click **Deploy**.
5. Under **Layers**, add the layer version you just created.
6. Under **Configuration → Environment variables**, add:
   | Key | Value |
   |---|---|
   | `S3_BUCKET` | your bucket name |
   | `SSM_PARAMETER_NAME` | `/job-tracker/last-run-timestamp` |
   | `SECRETS_MANAGER_SECRET` | `job-tracker/gmail-oauth` |
   | `BEDROCK_MODEL_ID` | `us.anthropic.claude-haiku-4-5-20251001-v1:0` |
7. Under **Configuration → General configuration**, set **Timeout to 5 minutes** and **Memory to 256 MB** (defaults of 3 seconds / 128 MB are too low once a run processes more than a couple of emails).
8. Under **Runtime settings**, confirm the **Handler** is `lambda_function.handler` (the function in the code is named `handler`, not Lambda's default expected name `lambda_handler` — a mismatch here throws `Runtime.HandlerNotFound`).

---

## Step 8 — EventBridge Scheduler

1. **EventBridge → Scheduler → Create schedule.**
2. Name: `job-tracker-daily-trigger`.
3. Schedule pattern: **Recurring**, cron-based: `cron(59 23 * * ? *)`.
4. **Time zone: `America/New_York`** (not UTC — this is what makes 11:59pm track correctly through the EDT/EST daylight-saving switch automatically, no manual cron updates twice a year).
5. Flexible time window: **Off**.
6. Target: **AWS Lambda → Invoke**, select `job-tracker-daily-run`. Default empty payload.
7. Let the console auto-generate a new invoke role scoped to `lambda:InvokeFunction` on this one function.

---

## Step 9 — QuickSight dataset

> QuickSight was rebranded **Amazon Quick (Quick Suite)** around October 2025. The classic BI functionality (Data / Analyses / Dashboards) still lives under those names in the left sidebar, ignore the newer chat-first landing page, that's a separate feature.

1. If this is your first time using QuickSight in this account, sign up for the **Standard** edition (cheaper, sufficient for a personal dashboard — Enterprise adds features like public link sharing that aren't needed here).
2. **Grant S3 access:** account icon (top right) → **Manage account** → **AWS resources** shortcut → find Amazon S3 → check your bucket. (This replaced the older "Manage QuickSight → Security & permissions" path.)
3. **Create the manifest file** (see `job-data-manifest.json` in this repo):
   ```json
   {
     "fileLocations": [
       { "URIPrefixes": ["s3://YOUR_BUCKET_NAME/job-data/"] }
     ],
     "globalUploadSettings": { "format": "JSON" }
   }
   ```
4. Upload the manifest to S3 (e.g. `s3://YOUR_BUCKET_NAME/manifest/job-data-manifest.json`).
5. **Data → Create dataset → S3**, name it `job-tracker-data`, paste the manifest's S3 HTTPS URL, click **Connect**.
6. Set up a **scheduled SPICE refresh** for `00:01 America/New_York`, two minutes after the nightly Lambda run, so the dashboard picks up new data automatically with no manual steps. (Manual refresh, if you ever need it, is triggered from the **Data** list by clicking the dataset **name** — the row's three-dot menu only has Edit/Duplicate/Delete, no refresh option there.)

---

## Step 10 — Dashboard visuals

Build one analysis on top of the `job-tracker-data` dataset. Two calculated fields solve a duplicate-counting problem first (one real application generates several emails — confirmation, interview, rejection — all separate rows):

```
latest_date_per_application = maxOver({date_received}, [{company}, {role}], PRE_AGG)
is_latest_update = ifelse({date_received} = {latest_date_per_application}, 1, 0)
```

`is_latest_update = 1` collapses a multi-email thread down to just its current status. A third calculated field powers a day/month slicer:

```
granular_date = ifelse(
  {DateGranularity} = 'Month',
  truncDate('MM', {date_received}),
  truncDate('DD', {date_received})
)
```

This needs a parameter `DateGranularity` (String, **Single value**, static values `Day` / `Month`, default `Day`) and a **List** or **Dropdown** control bound to it, with the same `Day` / `Month` values defined on the *control* (not the parameter's default field, those are two separate places to enter values, and it's easy to put them in the wrong one).

**The five visuals:**
1. **Email Status Percentages** (pie chart) — status breakdown, filtered to `is_latest_update = 1`.
2. **Action Required** (bar chart) — `requires_action` true/false, all emails, not filtered.
3. **Status Count by Date** (line chart) — X axis on `granular_date`, driven by the Day/Month slicer.
4. **Roles Applied to** (word cloud) — filtered to `is_latest_update = 1`.
5. **Companies applied to** (word cloud) — filtered to `is_latest_update = 1`.

Plus a **table** of `company, role, status, summary` filtered to `requires_action = true`, originally built just to verify the classification logic, ended up useful enough to keep permanently, it's a genuinely actionable "reply to these" list.

---

## Classification logic explained

Every fetched email is sent to Bedrock with a prompt asking for this fixed JSON schema:

```
company, role, status, date_received, sender_email, summary, requires_action
```

**`status`** is one of: `applied_confirmation`, `interview_scheduled`, `assessment_requested`, `rejected`, `offer`, `networking_outreach`, `not_job_related`, `other`.

- The **not_job_related vs. everything else** test: is this email about a job application, whether from a person or an automated ATS (Greenhouse, Workday, Lever, etc)? Automated/no-reply senders are treated as the *norm* for rejections and status updates, not a red flag.
- **`networking_outreach`** specifically means a genuine one-on-one exchange with a *named individual* about a referral/favor, including short reply threads. It explicitly excludes mass recruiter/staffing marketing blasts, those go to `other`.

**`requires_action`** is direction-aware:
- Always `false` for `not_job_related`.
- If the email was **sent by the account owner**, `false` by default (the action was already taken by sending it), unless it's a reminder about something still owed.
- If **received**, `true` only if it's genuinely waiting on the user (confirming an interview time, completing an assessment, responding to an offer); `false` if purely informational (a rejection, an automated confirmation) or the ball isn't in the user's court.
- Direction is determined by comparing the Gmail `From` header against the account's own address (fetched dynamically, not hardcoded), never left for the model to infer from tone alone.

---

## Testing and backfilling

To manually test: **Lambda → Test tab → Create test event** (payload content doesn't matter, the handler ignores it) → **Test**. Check the response (`emails_fetched`, `job_related_records`, `s3_keys`), CloudWatch Logs for errors, and the S3 bucket for the written file.

To backfill older history, temporarily roll `/job-tracker/last-run-timestamp` back to an earlier ISO timestamp (e.g. `2026-07-01T00:00:00+00:00`) and re-run the test — the deduplication logic (see Bugs section) makes this safe to re-run without creating duplicate records.

---

## Bugs we hit and how they were fixed

Real, diagnosed-from-actual-data bugs, keeping them documented so they aren't repeated if this pipeline gets extended.

1. **Every email classified `not_job_related`.** Claude wrapped its JSON response in markdown fences despite instructions not to, so `json.loads` failed silently on every call. Fixed by stripping fences before parsing, plus a permanent low-noise warning log if parsing ever fails again.
2. **Automated ATS rejections/updates still misclassified.** An earlier "default to not_job_related when unsure" rule combined badly with automated senders reading as "ambiguous." Fixed by telling the model explicitly that automated senders are the norm for this category.
3. **Duplicate records** (one test showed 136 records vs. ~29 actually unique). Repeated backfill tests re-fetched and re-wrote the same emails. Fixed by capturing Gmail's own `gmail_message_id` and deduplicating against it on every S3 write.
4. **`GetObject` returning a misleading `AccessDenied`** instead of a clean 404 on not-yet-existing keys, caused by missing `s3:ListBucket` permission (see Step 5).
5. **`requires_action` flagging the user's own sent emails.** A verification table showed many "true" records were the user's own outreach messages. Fixed with the direction-aware logic described above.
6. **Records bundled under the wrong date during backfills.** `write_to_s3` originally named files after the *run* date rather than each record's own `date_received`, so a multi-week backfill dumped everything into one file. Fixed to group by each record's actual date.
7. **Deployment quirks:** Lambda layer built on Windows needs `--platform manylinux2014_x86_64 --only-binary=:all:` or the compiled `cryptography` binary won't load; PowerShell's `Compress-Archive` silently mishandled that binary when zipping (use Python's `shutil.make_archive` instead); function architecture must match whatever the layer was built for; handler name in Runtime settings must match the actual function name in code; Claude Haiku 4.5 requires the cross-region inference profile ID, not the bare model ID; first-time Anthropic model use on Bedrock requires accepting an AWS Marketplace subscription (needs `aws-marketplace:ViewSubscriptions` / `Subscribe` permissions).

---

## Security notes

- The Lambda execution role is scoped to exactly four things: the specific Bedrock model, the `job-data` S3 prefix, one SSM parameter, and one Secrets Manager secret. Nothing broader.
- QuickSight's own role is separately scoped to read-only access on the same bucket.
- `client_secret.json`, the Gmail refresh token, and anything else sensitive are **never committed to this repo** — see `.gitignore`. All secrets live in AWS Secrets Manager; all configuration lives in Lambda environment variables, referenced by name only.
- If you fork or reuse this, you'll need your own Gmail OAuth credentials, your own S3 bucket, and your own AWS account resources, none of the ARNs, bucket names, or IDs in this README are live/functional outside the original author's account.

---

## Repo structure

```
.
├── README.md                          — this file
├── lambda_function.py                 — the Lambda function
├── get_gmail_refresh_token.py         — one-time local script for Gmail OAuth (Step 1)
├── requirements.txt                   — dependencies for the Lambda layer
├── job-tracker-lambda-permissions.json — IAM inline policy (Step 5)
├── job-data-manifest.json             — QuickSight manifest file (Step 9)
└── .gitignore                         — excludes secrets, venv, build artifacts
```