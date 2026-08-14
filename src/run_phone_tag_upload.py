"""Apply Trestle dial-tier tags to phones in ty+2 via the 'Tagging phones by phone
numbers' update wizard, from a Phone Number / Phone Tags CSV.

  python src/run_phone_tag_upload.py                 # dry (stop at Review, screenshots)
  python src/run_phone_tag_upload.py --finish        # commit
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
from sift_upload_wizard import run_phone_tag_upload  # noqa: E402

SHOT = Path("output/_phonetag_run")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="output/ftm_phone_tiers.csv")
    ap.add_argument("--finish", action="store_true")
    ap.add_argument("--headed", action="store_true")
    a = ap.parse_args()
    SHOT.mkdir(parents=True, exist_ok=True)
    mode = "COMMIT (Finish Upload)" if a.finish else "DRY (stop at Review)"
    print(f"Phone-tag upload: {a.csv} | {mode} | account={config.DATASIFT_EMAIL}")
    res = asyncio.run(run_phone_tag_upload(
        a.csv, do_finish=a.finish, headless=not a.headed, shot_base=str(SHOT / "run")))
    print("RESULT:", res)


if __name__ == "__main__":
    main()
