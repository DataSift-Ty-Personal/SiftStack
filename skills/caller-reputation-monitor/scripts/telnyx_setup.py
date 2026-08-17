"""
telnyx_setup.py - ONE-TIME Telnyx Number Reputation onboarding (run when creds land).

Walks the enterprise -> LOA -> enable -> associate flow from TELNYX-API-CONTRACT.md.
Each step is a subcommand so you can resume: the enterprise_id and loa_document_id are
written to config/telnyx.json and reused. Nothing here runs in the daily cron.

Prereq: TELNYX_API_KEY in .env (next to the scripts) (or env), and API access to the Telnyx account
that owns the DIDs (Section 7 of the handover).

Steps:
  1. python telnyx_setup.py register --identity config/enterprise.json
       POST /v2/enterprises           -> enterprise_id (FREE). Copy enterprise.example.json first.
  2. python telnyx_setup.py loa --out loa.pdf
       POST .../reputation/loa        -> renders the LOA PDF. Sign it (DocuSign/print+sign+scan).
  3. python telnyx_setup.py upload-loa --file loa_signed.pdf
       POST /v2/documents             -> loa_document_id (FREE).
  4. python telnyx_setup.py enable --frequency weekly
       POST .../reputation            -> BILLABLE: starts the $100/mo base. Waits on approval.
  5. python telnyx_setup.py status
       GET .../reputation             -> shows status + loa_status (both must be 'approved').
  6. python telnyx_setup.py associate
       POST .../reputation/numbers    -> adds the numbers.json DIDs (<=100/batch).

After step 6, add TELNYX_ENTERPRISE_ID (printed by step 1/5) to .env (next to the scripts) and the
daily monitor picks up L2 automatically.

Stdlib only. Multipart upload for the signed LOA is built by hand (no `requests`).
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

SUBDIR = Path(__file__).resolve().parent
CONFIG_DIR = SUBDIR / "config"
NUMBERS_FILE = CONFIG_DIR / "numbers.json"
TELNYX_STATE = CONFIG_DIR / "telnyx.json"     # {enterprise_id, loa_document_id, check_frequency}
ENV_FILE = Path(os.environ.get("REPUTATION_ENV_FILE", str(SUBDIR / ".env")))

API_BASE = "https://api.telnyx.com/v2"


def load_env_file() -> dict:
    env = {}
    if ENV_FILE.exists():
        for raw in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def api_key() -> str:
    env = load_env_file()
    k = env.get("TELNYX_API_KEY") or os.environ.get("TELNYX_API_KEY")
    if not k:
        print("FATAL: TELNYX_API_KEY not set in .env (next to the scripts) or env.")
        sys.exit(2)
    return k


def load_json(path: Path, default=None):
    if not path.exists():
        return default
    txt = path.read_text(encoding="utf-8-sig").strip()
    return json.loads(txt) if txt else default


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def _req(method: str, url: str, key: str, *, body: dict | None = None,
         raw_body: bytes | None = None, content_type: str | None = None,
         timeout: int = 60):
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    elif raw_body is not None:
        data = raw_body
        if content_type:
            headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body_bytes = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            return {"ok": True, "status": resp.status, "bytes": body_bytes,
                    "ctype": ctype}
    except urllib.error.HTTPError as e:
        txt = ""
        try:
            txt = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "status": e.code, "error": f"HTTP {e.code}: {txt[:600]}"}
    except (urllib.error.URLError, TimeoutError) as e:
        return {"ok": False, "status": None, "error": str(e)}


def _json(res):
    try:
        return json.loads(res["bytes"].decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return {}


def _state() -> dict:
    return load_json(TELNYX_STATE, {}) or {}


def _need_enterprise() -> str:
    st = _state()
    ent = st.get("enterprise_id") or load_env_file().get("TELNYX_ENTERPRISE_ID")
    if not ent:
        print("FATAL: no enterprise_id. Run `register` first (or set TELNYX_ENTERPRISE_ID).")
        sys.exit(2)
    return ent


# ---- steps ----
def cmd_register(args):
    key = api_key()
    identity = load_json(Path(args.identity))
    if not identity:
        print(f"FATAL: identity file {args.identity} not found/empty. "
              "Copy config/enterprise.example.json -> config/enterprise.json and fill it.")
        sys.exit(2)
    res = _req("POST", f"{API_BASE}/enterprises", key, body=identity)
    if not res["ok"]:
        print("register FAILED:", res["error"])
        sys.exit(1)
    data = _json(res).get("data", {}) or {}
    ent = data.get("id")
    st = _state()
    st["enterprise_id"] = ent
    save_json(TELNYX_STATE, st)
    print(f"OK. enterprise_id = {ent}")
    print("Add to .env (next to the scripts):  TELNYX_ENTERPRISE_ID=" + str(ent))


def cmd_loa(args):
    key = api_key()
    ent = _need_enterprise()
    body = {}
    if args.signer_name:
        body["signature"] = {"signer_name": args.signer_name}
    res = _req("POST", f"{API_BASE}/enterprises/{ent}/reputation/loa", key,
               body=body if body else None)
    if not res["ok"]:
        print("loa render FAILED:", res["error"])
        sys.exit(1)
    if "pdf" not in (res.get("ctype") or "").lower():
        # Some accounts may return JSON with a link; surface it.
        print("Note: response was not a PDF. Body:")
        print(res["bytes"].decode("utf-8", "replace")[:800])
        sys.exit(1)
    out = Path(args.out)
    out.write_bytes(res["bytes"])
    print(f"OK. LOA rendered -> {out}. Sign it, then: upload-loa --file <signed>.pdf")


def cmd_upload_loa(args):
    key = api_key()
    path = Path(args.file)
    if not path.exists():
        print(f"FATAL: {args.file} not found.")
        sys.exit(2)
    # hand-rolled multipart/form-data (stdlib only)
    boundary = "----telnyxloa" + uuid.uuid4().hex
    ctype = mimetypes.guess_type(str(path))[0] or "application/pdf"
    filename = path.name
    parts = []
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        .encode())
    parts.append(f"Content-Type: {ctype}\r\n\r\n".encode())
    parts.append(path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    payload = b"".join(parts)
    res = _req("POST", f"{API_BASE}/documents", key, raw_body=payload,
               content_type=f"multipart/form-data; boundary={boundary}")
    if not res["ok"]:
        print("upload-loa FAILED:", res["error"])
        sys.exit(1)
    doc_id = (_json(res).get("data", {}) or {}).get("id")
    st = _state()
    st["loa_document_id"] = doc_id
    save_json(TELNYX_STATE, st)
    print(f"OK. loa_document_id = {doc_id}. Next: enable --frequency weekly")


def cmd_enable(args):
    key = api_key()
    ent = _need_enterprise()
    st = _state()
    doc = st.get("loa_document_id")
    if not doc:
        print("FATAL: no loa_document_id. Run upload-loa first.")
        sys.exit(2)
    body = {"loa_document_id": doc, "check_frequency": args.frequency}
    print("This is BILLABLE: enabling reputation starts the ~$100/mo base fee.")
    res = _req("POST", f"{API_BASE}/enterprises/{ent}/reputation", key, body=body)
    if not res["ok"]:
        print("enable FAILED:", res["error"])
        sys.exit(1)
    st["check_frequency"] = args.frequency
    save_json(TELNYX_STATE, st)
    print("OK. Reputation enable submitted. Poll with: status")


def cmd_status(args):
    key = api_key()
    ent = _need_enterprise()
    res = _req("GET", f"{API_BASE}/enterprises/{ent}/reputation", key)
    if not res["ok"]:
        print("status FAILED:", res["error"])
        sys.exit(1)
    data = _json(res).get("data", _json(res))
    status = data.get("status")
    loa_status = data.get("loa_status")
    print(json.dumps({"enterprise_id": ent, "status": status,
                      "loa_status": loa_status,
                      "check_frequency": data.get("check_frequency")}, indent=2))
    if status == "approved" and loa_status == "approved":
        print("BOTH gates approved. You can now: associate")
    else:
        print("Not fully approved yet (vetting takes minutes). Re-run status.")


def cmd_associate(args):
    key = api_key()
    ent = _need_enterprise()
    cfg = load_json(NUMBERS_FILE, {}) or {}
    excluded = set(cfg.get("excluded", []) or [])
    nums = []
    for e in cfg.get("numbers", []):
        raw = e.get("number", "")
        d = "".join(ch for ch in str(raw) if ch.isdigit())
        if len(d) == 10:
            d = "1" + d
        if len(d) == 11 and d.startswith("1"):
            e164 = "+" + d
            if e164 not in excluded:
                nums.append(e164)
    nums = sorted(set(nums))
    if not nums:
        print("No DIDs in numbers.json to associate.")
        return
    # batch by 100
    ok_all = True
    for i in range(0, len(nums), 100):
        batch = nums[i:i + 100]
        res = _req("POST", f"{API_BASE}/enterprises/{ent}/reputation/numbers", key,
                   body={"phone_numbers": batch})
        if res["ok"]:
            print(f"associated {len(batch)}: {batch[0]}..{batch[-1]}")
        else:
            ok_all = False
            print(f"associate batch FAILED ({batch[0]}..): {res['error']}")
        time.sleep(0.5)
    print("DONE." if ok_all else "DONE with errors (association is all-or-nothing per batch).")


def main() -> int:
    ap = argparse.ArgumentParser(description="Telnyx Number Reputation one-time setup")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("register"); p.add_argument("--identity", default="config/enterprise.json")
    p = sub.add_parser("loa"); p.add_argument("--out", default="loa.pdf"); p.add_argument("--signer-name", default="")
    p = sub.add_parser("upload-loa"); p.add_argument("--file", required=True)
    p = sub.add_parser("enable"); p.add_argument("--frequency", default="weekly",
        choices=["business_daily", "daily", "weekly", "biweekly", "monthly", "never"])
    sub.add_parser("status")
    sub.add_parser("associate")

    args = ap.parse_args()
    return {
        "register": cmd_register, "loa": cmd_loa, "upload-loa": cmd_upload_loa,
        "enable": cmd_enable, "status": cmd_status, "associate": cmd_associate,
    }[args.cmd](args) or 0


if __name__ == "__main__":
    sys.exit(main())
