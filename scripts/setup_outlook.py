"""
scripts/setup_outlook.py
=========================
One-time OAuth2 setup for the Outlook/Law360 newsletter scraper.

Run this script once from a terminal that can display a URL. It will:
  1. Start a Microsoft device-code flow
  2. Print a short URL + one-time code to paste into any browser
  3. Wait for you to sign in as Ohad@levin-telraz.co.il
  4. Save the refresh token to data/outlook_token.json

After this, scrapers/outlook_law360.py will silently refresh the token
on every run — you only need to repeat this setup if the refresh token
expires (typically after 90 days of inactivity) or is revoked.

Prerequisites
-------------
  pip install "msal>=1.28"

Environment variables (set in .env or shell before running):
  OUTLOOK_CLIENT_ID   — Azure app client ID  (required)
  OUTLOOK_TENANT_ID   — tenant ID or "common" (default: common)

Azure app registration checklist
---------------------------------
  1. portal.azure.com → App registrations → New registration
  2. Name: "Class Action Scout"   Supported account types: "Single tenant"
     (or "Accounts in this org" if on Microsoft 365 Business)
  3. Redirect URI → Public client / native → http://localhost
  4. API permissions → Microsoft Graph → Delegated → Mail.Read → Grant admin consent
  5. Authentication → Allow public client flows → YES
  6. Copy the Application (client) ID and Tenant ID into your .env
"""
import os
import sys
from pathlib import Path

# Allow running from repo root without installing the package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Load .env
_env = Path(__file__).resolve().parent.parent / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

from scrapers.outlook_law360 import OutlookTokenManager, TOKEN_PATH, SCOPES


def main() -> None:
    client_id = os.getenv("OUTLOOK_CLIENT_ID", "").strip()
    tenant_id = os.getenv("OUTLOOK_TENANT_ID", "common").strip()
    user_email = os.getenv("OUTLOOK_USER_EMAIL", "Ohad@levin-telraz.co.il").strip()

    if not client_id:
        print(
            "\n[ERROR] OUTLOOK_CLIENT_ID is not set.\n"
            "Add it to your .env file:\n\n"
            "  OUTLOOK_CLIENT_ID=<your-azure-app-client-id>\n"
            "  OUTLOOK_TENANT_ID=<your-tenant-id-or-common>\n\n"
            "See the docstring in this file for the Azure registration steps.\n"
        )
        sys.exit(1)

    print(f"\nClass Action Scout — Outlook OAuth Setup")
    print(f"User email : {user_email}")
    print(f"Client ID  : {client_id}")
    print(f"Tenant     : {tenant_id}")
    print(f"Token file : {TOKEN_PATH}\n")

    # Check for msal
    try:
        import msal
    except ImportError:
        print("[ERROR] msal is not installed. Run:\n  pip install 'msal>=1.28'\n")
        sys.exit(1)

    print("Starting device-code flow...\n")
    tm = OutlookTokenManager(client_id, tenant_id, TOKEN_PATH)

    # Always force a fresh device flow during setup so the correct account is used
    try:
        token = tm.acquire_token_device_flow()
    except RuntimeError as e:
        print(f"\n[ERROR] {e}")
        sys.exit(1)

    print("\n✓ Authentication successful.")
    print(f"✓ Token saved to: {TOKEN_PATH}")

    # Quick smoke-test: list the 3 most recent Law360 emails
    print("\nVerifying access — searching for Law360 emails...")
    try:
        import requests
        from datetime import datetime, timedelta, timezone

        from urllib.parse import quote
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        since = cutoff.strftime("%Y-%m-%d") + "T00:00:00Z"
        odata_filter = (
            f"from/emailAddress/address eq 'news-alt@law360.com'"
            f" and receivedDateTime ge {since}"
        )
        # Build query string manually — preserves '$' in OData key names and
        # uses %20 (not '+') for spaces, which Graph's OData parser requires.
        qs = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in {
            "$filter": odata_filter,
            "$select": "subject,receivedDateTime",
            "$top": "3",
        }.items())
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        resp = requests.get(
            f"https://graph.microsoft.com/v1.0/me/messages?{qs}",
            headers=headers,
            timeout=20,
        )
        resp.raise_for_status()
        msgs = resp.json().get("value", [])
        if msgs:
            print(f"\nFound {len(msgs)} recent Law360 email(s):")
            for m in msgs:
                print(f"  · {m.get('receivedDateTime','')[:10]}  {m.get('subject','')}")
        else:
            print("\nNo Law360 emails found in the last 30 days for this mailbox.")
            print("(This is fine — the scraper will check on every run.)")
    except Exception as e:
        print(f"\n[WARN] Smoke-test request failed: {e}")
        print("Token was saved; the scraper should still work on the next run.")

    print("\nSetup complete. You can now run the scraper:\n  python scheduler.py\n")


if __name__ == "__main__":
    main()
