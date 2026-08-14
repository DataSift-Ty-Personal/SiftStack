"""One-time: mint a PERMANENT Dropbox refresh token for the FTM pipeline.

The pipeline hosts auction-notice screenshots on Dropbox and needs a long-lived
refresh token (NOT a short-lived `sl.*` access token, which dies in ~4 hours).
Run this ONCE in your own terminal, authorize in the browser, paste the code,
then copy the printed refresh token into .env as DROPBOX_REFRESH_TOKEN.

    cd C:\\Users\\Tyrus\\OneDrive\\SiftStack
    python src\\dropbox_get_refresh_token.py

Uses the existing DROPBOX_APP_KEY / DROPBOX_APP_SECRET from .env. If the authorize
page errors about scopes, open the Dropbox App Console -> your app -> Permissions,
enable: files.content.write, sharing.write, sharing.read, click Submit, then re-run.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402

try:
    from dropbox import DropboxOAuth2FlowNoRedirect
except ImportError:
    print("The 'dropbox' package isn't installed for this interpreter.")
    print("Run with the SiftStack venv:  .venv\\Scripts\\python.exe src\\dropbox_get_refresh_token.py")
    sys.exit(1)

# Minimal scopes the screenshot uploader actually uses: upload the PNG, create a
# public share link, and (on idempotent re-runs) look up the existing link.
SCOPES = ["files.content.write", "sharing.write", "sharing.read"]


def main():
    if not (config.DROPBOX_APP_KEY and config.DROPBOX_APP_SECRET):
        print("DROPBOX_APP_KEY / DROPBOX_APP_SECRET must be set in .env first.")
        sys.exit(1)

    flow = DropboxOAuth2FlowNoRedirect(
        config.DROPBOX_APP_KEY,
        config.DROPBOX_APP_SECRET,
        token_access_type="offline",   # <-- this is what yields a REFRESH token
        scope=SCOPES,
    )
    authorize_url = flow.start()
    print("=" * 70)
    print("STEP 1  Open this URL in your browser and click 'Allow':\n")
    print("  " + authorize_url + "\n")
    print("STEP 2  Copy the authorization code Dropbox shows you.")
    print("=" * 70)
    code = input("STEP 3  Paste the code here, then Enter: ").strip()

    try:
        res = flow.finish(code)
    except Exception as e:  # noqa: BLE001
        print("\nFailed to exchange the code: " + str(e))
        print("Make sure the code was pasted exactly and the app has the scopes above.")
        sys.exit(2)

    print("\n" + "=" * 70)
    print("SUCCESS. Paste this line into your .env (replace the old value):\n")
    print("DROPBOX_REFRESH_TOKEN=" + res.refresh_token)
    print("\n(For reference, a refresh token is short and does NOT start with 'sl.')")
    print("=" * 70)


if __name__ == "__main__":
    main()
