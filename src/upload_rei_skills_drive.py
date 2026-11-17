"""Upload the 13 current REI skill files to a Google Drive folder and make them
viewable by anyone with the link. Prints the folder link + one share link per file.

Requires in .env:
    GOOGLE_SERVICE_ACCOUNT_KEY   base64-encoded service account JSON key
    GOOGLE_DRIVE_FOLDER_ID       (optional) parent folder ID; the new folder is
                                 created inside it so it shows up in your Drive.
                                 The parent folder must be shared with the
                                 service account email as Editor.

Run from project root:
    python src/upload_rei_skills_drive.py
"""
import base64
import json
import sys
from pathlib import Path

from dotenv import load_dotenv
import os

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

REPO = Path(__file__).resolve().parent.parent
SKILL_DIR = REPO / "Skills for REI" / "improved"
FOLDER_NAME = "REI Skill Library"

# Current release set (deep-prospecting-v4.skill supersedes deep-prospecting.skill)
SKILL_FILES = [
    "sift-market-research.skill",
    "first-market-county-data.skill",
    "buyer-prospector.skill",
    "real-estate-comping.skill",
    "rehab-estimator.skill",
    "deal-analyzer.plugin",
    "deep-prospecting-v4.skill",
    "probate-property-finder.skill",
    "phone-validator.skill",
    "sequential-presets.skill",
    "sift-sequences.skill",
    "sift-operations.plugin",
    "playbook-creator.skill",
]


def main() -> int:
    load_dotenv(REPO / ".env")
    sa_b64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_KEY", "").strip()
    parent_folder = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "").strip()
    if not sa_b64:
        print("ERROR: GOOGLE_SERVICE_ACCOUNT_KEY is not set in .env")
        return 1

    key_json = json.loads(base64.b64decode(sa_b64))
    creds = Credentials.from_service_account_info(
        key_json, scopes=["https://www.googleapis.com/auth/drive.file"]
    )
    svc = build("drive", "v3", credentials=creds, cache_discovery=False)

    folder_meta = {"name": FOLDER_NAME, "mimeType": "application/vnd.google-apps.folder"}
    if parent_folder:
        folder_meta["parents"] = [parent_folder]
    folder = svc.files().create(body=folder_meta, fields="id, webViewLink").execute()
    folder_id = folder["id"]

    svc.permissions().create(
        fileId=folder_id, body={"type": "anyone", "role": "reader"}
    ).execute()
    print(f"FOLDER  {FOLDER_NAME}")
    print(f"        {folder.get('webViewLink')}")
    print()

    failures = []
    for name in SKILL_FILES:
        path = SKILL_DIR / name
        if not path.exists():
            failures.append((name, "missing locally"))
            continue
        try:
            media = MediaFileUpload(str(path), mimetype="application/zip", resumable=True)
            f = svc.files().create(
                body={"name": name, "parents": [folder_id]},
                media_body=media,
                fields="id, webViewLink",
            ).execute()
            # Explicit per-file permission so direct links work regardless of inheritance
            svc.permissions().create(
                fileId=f["id"], body={"type": "anyone", "role": "reader"}
            ).execute()
            print(f"{name}")
            print(f"        {f.get('webViewLink')}")
        except Exception as e:  # noqa: BLE001
            failures.append((name, str(e)[:200]))

    if failures:
        print("\nFAILURES:")
        for name, err in failures:
            print(f"  {name}: {err}")
        return 2
    print(f"\nDone: {len(SKILL_FILES)} files uploaded, all viewable by anyone with the link.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
