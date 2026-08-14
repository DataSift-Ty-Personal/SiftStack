"""Proof: upload an existing notice screenshot to Dropbox, get a PERMANENT public
link, apply the direct-render transform, and verify it serves the raw PNG.
Validates the whole 'auction image -> durable public URL' premise before wiring.
"""
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config  # noqa: E402
from dropbox_uploader import upload_and_share  # noqa: E402


def direct_url(u: str) -> str:
    """Turn a Dropbox share link (preview, ?dl=0) into a direct inline-render URL."""
    if not u:
        return u
    if "dl=0" in u:
        return u.replace("dl=0", "raw=1")
    if "dl=1" in u:
        return u.replace("dl=1", "raw=1")
    if "raw=1" in u:
        return u
    return u + ("&" if "?" in u else "?") + "raw=1"


def main():
    png = Path("output/notices/notice_540224.png")
    print("creds present:",
          "refresh_token" if config.DROPBOX_REFRESH_TOKEN else
          ("access_token-env" if __import__("os").environ.get("DROPBOX_ACCESS_TOKEN") else "NONE"),
          "| app_key:", bool(config.DROPBOX_APP_KEY))
    print("PNG exists:", png.exists(), "->", png)
    if not png.exists():
        print("ABORT: test PNG missing"); return

    share = upload_and_share(png, "/FTM Auction Notices/_dropbox_proof_notice_540224.png")
    print("share url :", share)
    if not share:
        print("ABORT: upload_and_share returned None (creds/network?)"); return
    direct = direct_url(share)
    print("direct url:", direct)

    req = urllib.request.Request(direct, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
        print("HTTP", r.status, "| content-type:", r.headers.get("content-type"), "| bytes:", len(data))
        print("PNG magic OK:", data[:8] == b"\x89PNG\r\n\x1a\n")
        print("RESULT:", "PASS - permanent public image link works"
              if (r.status == 200 and data[:8] == b"\x89PNG\r\n\x1a\n") else "FAIL")


if __name__ == "__main__":
    main()
