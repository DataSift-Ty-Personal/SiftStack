"""DataSift Open API client for the sift-sequences skill.

Every request carries your DataSift Open API key in the header
"Authorization: Api-Key <key>". An account admin creates the key in DataSift
(developers.datasift.ai explains it), and it never expires.

The key is read from the REISIFT_API_KEY environment variable, or from a .env
file holding REISIFT_API_KEY=<your key> in the folder you run from or next to
this script.

This skill only sends GET requests: reading what the account holds never
changes it. Sequences are built by hand in the Sequence Builder (see SKILL.md),
because they are not in DataSift's published API. DataSift allows 120
requests a minute per account, shared by every tool on it, so the client paces
itself at about 80 and waits when DataSift asks it to (HTTP 429).
Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://apiv2.reisift.io"
GAP_SECONDS = 0.75          # about 80 requests a minute


class ApiError(RuntimeError):
    """DataSift answered with an error, or could not be reached."""

    def __init__(self, code: int, path: str, body: str = ""):
        super().__init__(f"HTTP {code} on {path}: {body[:200]}")
        self.code, self.path, self.body = code, path, body


def api_key() -> str:
    """The Open API key from REISIFT_API_KEY, else from a .env file. Raises SystemExit when absent."""
    key = os.environ.get("REISIFT_API_KEY", "").strip()
    if key:
        return key
    for f in (Path.cwd() / ".env", Path(__file__).resolve().parent / ".env"):
        if not f.is_file():
            continue
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            k, _, v = line.partition("=")
            if k.strip() == "REISIFT_API_KEY" and v.strip():
                return v.strip().strip('"').strip("'")
    raise SystemExit("No API key. Set REISIFT_API_KEY to your DataSift Open API key. "
                     "An account admin creates it in DataSift (developers.datasift.ai explains it).")


class DataSiftApi:
    """GET requests on the Open API key, paced, with DataSift's own wait hint honored."""

    def __init__(self, key: str | None = None, gap: float = GAP_SECONDS):
        self.key = key or api_key()
        self.gap = gap
        self.calls = 0
        self._last = 0.0

    def get(self, path: str, params: dict | None = None, tries: int = 6):
        url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        for attempt in range(tries):
            wait = self.gap - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.calls += 1
            req = urllib.request.Request(url, method="GET", headers={
                "Authorization": f"Api-Key {self.key}", "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as e:
                body = (e.read() or b"").decode("utf-8", "replace")
                if e.code == 401:
                    raise SystemExit("The API key was refused (HTTP 401). Check REISIFT_API_KEY: it must "
                                     "be your DataSift Open API key.") from None
                if e.code == 429 and attempt < tries - 1:
                    m = re.search(r"available in (\d+)", body, re.I)
                    hint = e.headers.get("Retry-After") if e.headers else None
                    secs = int(m.group(1)) + 1 if m else (int(hint) if hint and hint.isdigit() else 2 ** attempt)
                    time.sleep(min(secs, 90))
                    continue
                if e.code in (502, 503, 504) and attempt < 2:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise ApiError(e.code, path, body) from None
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < 2:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise ApiError(0, path, str(e)) from None
        raise ApiError(0, path, f"gave up after {tries} tries")
