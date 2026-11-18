"""One-time Gmail login. Asks ONLY for permission to send email (scope gmail.send), writes token.json.

Run:  python scripts\\gmail_auth.py
A browser opens. "Google hasn't verified this app" is expected for your own test app: click Continue.

For Vercel: open token.json yourself and copy `client_id`, `client_secret` and `refresh_token` into the
Vercel environment variables GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET, GMAIL_REFRESH_TOKEN.
(This script never prints them.)
"""

import _bootstrap  # noqa: F401  (sets up imports + UTF-8 console)

import sys

from outbound import PROJECT_ROOT
from outbound.config import get_settings
from outbound.send import GMAIL_SCOPES


def main() -> int:
    s = get_settings()
    creds_file = PROJECT_ROOT / s.gmail_credentials_file
    token_file = PROJECT_ROOT / s.gmail_token_file
    if not creds_file.exists():
        print(f"❌ {creds_file.name} not found in the project folder. See docs/SETUP_WINDOWS.md step 7.")
        return 1
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(creds_file), GMAIL_SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
    token_file.write_text(creds.to_json(), encoding="utf-8")
    print(f"✅ Saved {token_file.name}. Scope: send email only. Run python scripts\\check_setup.py to confirm.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
