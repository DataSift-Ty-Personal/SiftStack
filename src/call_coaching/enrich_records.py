"""enrich_records.py - build a per-call record map {call_id: {address, owner,
reisift_url, contact}} for every graded call, so reports can show the PROPERTY
each call is about (linked to its reisift record) instead of a bare call id.

- reisift_url + contact come from output/call_coaching/call_log.json (authoritative).
- The property street address is spoken in the call, not structured, so it is
  extracted from each transcript with a cheap LLM pass (OpenRouter Gemini Flash),
  run in parallel. Result cached to output/call_coaching/record_map.json.

USAGE (from SiftStack root, venv python):
  python src/call_coaching/enrich_records.py            # all graded calls
  python src/call_coaching/enrich_records.py --force    # re-extract
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import sys
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "output" / "call_coaching"
TR_DIR = OUT / "transcripts"
REPORTS = OUT / "reports"
MAP_FILE = OUT / "record_map.json"
MODEL = "google/gemini-2.5-flash"
WORKERS = 6

PROMPT = """From this real estate cold-call transcript, extract the PROPERTY ADDRESS the agent is calling about. Return STRICT JSON only, no fences:
  address: the property street address as stated on the call, as complete as spoken (street number + name, plus city/state if given), or null if no address is ever stated
  owner: the property owner or contact name if identifiable, else null
Keep the address exactly as a human would write it (e.g. "531 Russell Road, Rockford, TN"). Do not invent digits. TRANSCRIPT:
"""


def _env(key: str) -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise KeyError(key)


def _extract(call_id: str) -> dict:
    md = TR_DIR / f"{call_id}.md"
    if not md.exists():
        return {"address": None, "owner": None}
    text = md.read_text(encoding="utf-8")
    body = text.split("## Transcript", 1)[-1][:12000]
    payload = {"model": MODEL, "messages": [{"role": "user", "content": PROMPT + body}]}
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {_env('OPENROUTER_API_KEY')}",
                 "Content-Type": "application/json"})
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                raw = json.loads(r.read())["choices"][0]["message"]["content"]
            m = re.search(r"\{.*\}", raw, re.S)
            if m:
                d = json.loads(m.group(0))
                return {"address": d.get("address"), "owner": d.get("owner")}
        except Exception:  # noqa: BLE001
            continue
    return {"address": None, "owner": None}


def graded_call_ids() -> list[str]:
    ids = set()
    for p in REPORTS.rglob("15*_*.md"):
        if "_archive" in str(p) or "docx" in p.parts:
            continue
        m = re.match(r"^(\d{9,})_", p.name)
        if m:
            ids.add(m.group(1))
    return sorted(ids)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    log = {str(c["call_id"]): c for c in
           json.loads((OUT / "call_log.json").read_text(encoding="utf-8"))}
    existing = json.loads(MAP_FILE.read_text(encoding="utf-8")) if MAP_FILE.exists() and not args.force else {}
    ids = graded_call_ids()
    todo = [i for i in ids if i not in existing or not existing[i].get("address")]
    print(f"{len(ids)} graded calls; extracting address for {len(todo)}")

    results = dict(existing)
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(_extract, i): i for i in todo}
        for n, fut in enumerate(concurrent.futures.as_completed(futs), 1):
            cid = futs[fut]
            ext = fut.result()
            c = log.get(cid, {})
            results[cid] = {
                "address": ext.get("address"),
                "owner": ext.get("owner") or c.get("contact_name"),
                "contact": c.get("contact_name"),
                "reisift_url": c.get("reisift_record_url"),
                "to_num": c.get("to_num"),
            }
            if n % 10 == 0:
                print(f"  {n}/{len(todo)}...", flush=True)

    MAP_FILE.write_text(json.dumps(results, indent=1), encoding="utf-8")
    got = sum(1 for i in ids if results.get(i, {}).get("address"))
    print(f"Wrote {MAP_FILE} ({got}/{len(ids)} with an address)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
