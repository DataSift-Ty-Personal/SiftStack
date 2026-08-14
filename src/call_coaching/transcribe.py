"""transcribe.py - transcribe SmrtPhone call recordings and classify each call.

Uses an audio-capable model (Gemini 2.5 Flash) through OpenRouter (OPENROUTER_API_KEY
in .env). Two passes per call:
  1. AUDIO -> transcript with speaker labels AND bracketed delivery notes
     (tone, pace, energy, silence). The model hears the audio, so tonality
     observations are real, not inferred from text.
  2. TEXT -> strict JSON classification: call_type (conversation / voicemail /
     no_contact / wrong_number / dead_air) + pipeline (cold_call /
     lead_management / closing) + has_seller_dialogue + one-line summary.

Cost is roughly $0.002 per minute of audio. Verified live 2026-07-06.

USAGE (from SiftStack root, venv python):
  python src/call_coaching/transcribe.py                 # everything in calls_to_review.json
  python src/call_coaching/transcribe.py --limit 5       # first 5 (test)
  python src/call_coaching/transcribe.py --call-id 151893352

Output per call:
  output/call_coaching/transcripts/{call_id}.md    transcript + metadata header
  output/call_coaching/transcripts/{call_id}.json  classification record
  output/call_coaching/review_queue.json           all classifications, grouped by pipeline
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "output" / "call_coaching"
REC_DIR = OUT_DIR / "recordings"
TR_DIR = OUT_DIR / "transcripts"
MODEL = "google/gemini-2.5-flash"
WORKERS = 4

# Role roster: overrides the LLM pipeline classification. These callers only
# make first-touch cold calls, so a "follow-up sounding" call is still a cold
# call (per Ty, 2026-07-07). Keys are lowercase caller names.
CALLER_ROLES = {
    "tinaa george": "cold_call",
    "adriana mondragon": "cold_call",
}

# Departed team members (per Ty, 2026-07-07): their historical calls are never
# queued for coaching review. Lowercase caller names.
EXCLUDED_CALLERS = {
    "javier marroquin",
    "john yunque",
}

TRANSCRIBE_PROMPT = """Transcribe this real estate investment phone call.
Rules:
- Label speakers AGENT and SELLER. AGENT is ALWAYS the company representative: the person calling about buying the property, asking if the owner wants to sell, or following up on a message about a property{agent_hint}. SELLER is ALWAYS the property owner or contact, even if the seller placed or returned the call and speaks first. Decide by what each person SAYS, not by who initiated. Use VOICEMAIL for an automated greeting and AGENT_VM for a voicemail message our caller leaves.
- One line per speaker turn: LABEL: text
- Add bracketed delivery notes inline where you actually hear them: [long pause 4s], [interrupts], [rushed], [monotone], [warm tone], [upswing on statement], [talking over], [laughs], [mumbled].
- After the transcript add a section exactly like:
DELIVERY SUMMARY:
- pace: (slow / conversational / rushed) plus one sentence
- energy and tone: one or two sentences on the agent specifically
- talk balance: rough percent agent vs seller
- notable audio moments: up to 3 bullets with timestamps
Output plain text only, no markdown fences."""

CLASSIFY_PROMPT = """You are triaging a transcribed outbound real estate call for coaching review.
Return STRICT JSON only, no fences, with keys:
  call_type: one of conversation | voicemail | no_contact | wrong_number | dead_air
    (conversation = a live two-way exchange with the target or a real decision path person, even short; wrong_number = live person but not the target and no property discussion possible)
  pipeline: one of cold_call | lead_management | closing
    (cold_call = first-touch intro to a property owner from a list; lead_management = follow-up or qualification of an existing lead, references prior contact or continues qualification; closing = offer, price negotiation, or contract talk)
  has_seller_dialogue: true|false (did the seller/target speak substantively)
  worth_grading: true|false (true only for conversation calls with seller dialogue)
  labels_swapped: true|false (true if the AGENT-labeled speaker is actually the property
    owner/contact and the SELLER-labeled speaker is the company caller; decide by content:
    our agent is whoever asks about buying/selling the property, regardless of label)
  summary: one sentence, plain
  seller_name_mentioned: string or null
TRANSCRIPT:
"""


def _log_recording_url(call_id) -> str | None:
    """Fallback: look the recording URL up in call_log.json when the review row lacks it."""
    log = OUT_DIR / "call_log.json"
    if not log.exists():
        return None
    for c in json.loads(log.read_text(encoding="utf-8")):
        if c.get("call_id") == call_id:
            return c.get("recording_url")
    return None


def _env(key: str) -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise KeyError(key)


def _openrouter(messages: list, timeout: int = 240) -> str:
    body = {"model": MODEL, "messages": messages}
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {_env('OPENROUTER_API_KEY')}",
                 "Content-Type": "application/json"})
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                resp = json.loads(r.read())
            content = (resp.get("choices") or [{}])[0].get("message", {}).get("content")
            if content:
                return content
            last = f"empty response: {json.dumps(resp)[:200]}"
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read().decode()[:200]}"
        except Exception as e:  # noqa: BLE001
            last = str(e)[:200]
    raise RuntimeError(f"OpenRouter failed after 3 tries: {last}")


def transcribe_audio(mp3: Path, agent_name: str | None = None) -> str:
    b64 = base64.b64encode(mp3.read_bytes()).decode()
    hint = f" (our agent is named {agent_name})" if agent_name else ""
    prompt = TRANSCRIBE_PROMPT.replace("{agent_hint}", hint)
    return _openrouter([{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "input_audio", "input_audio": {"data": b64, "format": "mp3"}},
    ]}])


def classify(transcript: str) -> dict:
    raw = _openrouter([{"role": "user", "content": CLASSIFY_PROMPT + transcript[:24000]}], timeout=90)
    m = re.search(r"\{.*\}", raw, re.S)
    return json.loads(m.group(0)) if m else {"call_type": "unknown", "worth_grading": False,
                                             "summary": raw[:200]}


def process(call: dict, force: bool = False) -> dict | None:
    if (call.get("caller") or "").lower() in EXCLUDED_CALLERS:
        return None
    cid = call["call_id"]
    mp3 = REC_DIR / f"{cid}.mp3"
    md = TR_DIR / f"{cid}.md"
    js = TR_DIR / f"{cid}.json"
    if not mp3.exists() or mp3.stat().st_size < 4096:
        return None
    if md.exists() and js.exists() and not force:
        return json.loads(js.read_text(encoding="utf-8"))
    text = transcribe_audio(mp3, call.get("caller"))
    cls = classify(text)
    role = CALLER_ROLES.get((call.get("caller") or "").lower())
    if role and cls.get("pipeline") != role:
        cls["pipeline_llm"] = cls.get("pipeline")
        cls["pipeline"] = role
    cls.update({"call_id": cid, "caller": call.get("caller"),
                "created_at_utc": call.get("created_at_utc"),
                "duration_seconds": call.get("duration_seconds"),
                "disposition": call.get("disposition"),
                "contact_name": call.get("contact_name"),
                "reisift_record_url": call.get("reisift_record_url"),
                "transcript_file": str(md)})
    recording = call.get("recording_url") or _log_recording_url(cid)
    header = "\n".join([
        f"# Call {cid}",
        f"- caller: {call.get('caller')}",
        f"- when (UTC): {call.get('created_at_utc')}",
        f"- duration: {call.get('duration_seconds')}s",
        f"- disposition: {call.get('disposition')}",
        f"- contact: {call.get('contact_name')}  ({call.get('to_num')})",
        f"- reisift record: {call.get('reisift_record_url')}",
        f"- recording: {recording}",
        f"- label check: {'SWAPPED - the AGENT lines below are the property owner; grade with corrected roles' if cls.get('labels_swapped') else 'ok'}",
        f"- classification: {cls.get('call_type')} / {cls.get('pipeline')} / worth_grading={cls.get('worth_grading')}",
        "", "## Transcript", "",
    ])
    md.write_text(header + text, encoding="utf-8")
    js.write_text(json.dumps(cls, indent=1), encoding="utf-8")
    return cls


def main() -> int:
    ap = argparse.ArgumentParser(description="Transcribe + classify call recordings")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--call-id", type=int)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    calls = json.loads((OUT_DIR / "calls_to_review.json").read_text(encoding="utf-8"))
    if args.call_id:
        calls = [c for c in calls if c["call_id"] == args.call_id]
    if args.limit:
        calls = calls[: args.limit]
    TR_DIR.mkdir(parents=True, exist_ok=True)

    results, errors = [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(process, c, args.force): c for c in calls}
        for i, fut in enumerate(concurrent.futures.as_completed(futs), 1):
            try:
                r = fut.result()
                if r:
                    results.append(r)
            except Exception as e:  # noqa: BLE001
                errors += 1
                print(f"  ERROR call {futs[fut]['call_id']}: {e}", file=sys.stderr)
            if i % 10 == 0:
                print(f"  {i}/{len(calls)} processed...", flush=True)

    queue = {"cold_call": [], "lead_management": [], "closing": [], "not_gradeable": []}
    for r in sorted(results, key=lambda x: x.get("created_at_utc") or ""):
        if r.get("worth_grading") and r.get("pipeline") in queue:
            queue[r["pipeline"]].append(r)
        else:
            queue["not_gradeable"].append(r)
    (OUT_DIR / "review_queue.json").write_text(json.dumps(queue, indent=1), encoding="utf-8")
    print(f"Transcribed {len(results)} calls ({errors} errors)")
    for k, v in queue.items():
        print(f"  {k}: {len(v)}")
    print(f"-> {OUT_DIR / 'review_queue.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
