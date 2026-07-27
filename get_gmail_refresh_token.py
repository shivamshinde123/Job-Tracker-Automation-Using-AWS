"""
One-time local script to get a Gmail OAuth refresh token.

Run this once on your own machine, not in Lambda. It opens a browser
window, asks you to log into your Gmail account, and grants read only
access. At the end it prints your refresh token, client ID, and client
secret, which you then paste into AWS Secrets Manager as a single
JSON secret.

Setup before running:
1. pip install google-auth-oauthlib google-auth-httplib2 --break-system-packages
2. Download your OAuth client JSON file from Google Cloud Console
   (APIs and Services > Credentials > your Desktop app OAuth client)
   and save it in this same folder as "client_secret.json".
"""

from google_auth_oauthlib.flow import InstalledAppFlow

# Read-only scope, since this pipeline only ever reads emails,
# it never sends, modifies, or deletes anything.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

CLIENT_SECRET_FILE = "client_secret.json"


def main():
    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)

    # This opens a local browser window. Log into the Gmail account
    # you want the pipeline to read from, and approve the read only
    # access request.
    credentials = flow.run_local_server(port=0)

    print("\n--- Save these three values into AWS Secrets Manager ---\n")
    print(f"gmail_client_id: {credentials.client_id}")
    print(f"gmail_client_secret: {credentials.client_secret}")
    print(f"gmail_refresh_token: {credentials.refresh_token}")
    print("\n---------------------------------------------------------\n")

    if not credentials.refresh_token:
        print(
            "WARNING: No refresh token was returned. This usually happens "
            "if you have run this script before and already granted access. "
            "Go to https://myaccount.google.com/permissions, remove access "
            "for this app, then run this script again."
        )


if __name__ == "__main__":
    main()
