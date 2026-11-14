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

THE BRAIN USES THIS MODULE TOO (src/agents/workflows/call_coaching.py, build plan 6.7). Three
things changed so it can, and the command line above works exactly as before:
  * The key is read from the environment first. A Fly secret arrives as an environment variable
    and the box has no .env, so the old .env-only read failed there before a single call.
  * transcribe_bytes() and classify_text() take audio already in memory and return what OpenRouter
    billed as well as the text, so a workflow can count the spend against its caps.
  * fetch_recording() reads a recording from the link the account's own call log carries. That link
    opens without a login; when one does not (a login page, a 401 or 403), it says so and the caller
    stops for that call. It never tries a cookie or a guess.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "output" / "call_coaching"
REC_DIR = OUT_DIR / "recordings"
TR_DIR = OUT_DIR / "transcripts"
MODEL = os.environ.get("CALL_COACHING_TRANSCRIBE_MODEL") or "google/gemini-2.5-flash"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
WORKERS = 4
# The one network call to OpenRouter, replaceable: the brain's selftest puts a fake here so no
# test can reach the service. None means the real request below.
POST = None

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
    """A setting: the environment first (Fly secrets), then the repo's .env on a workstation."""
    v = (os.environ.get(key) or "").strip()
    if v:
        return v
    f = ROOT / ".env"
    if f.is_file():
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise KeyError(key)


def _post_openrouter(body: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        OPENROUTER_URL, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {_env('OPENROUTER_API_KEY')}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _chat(messages: list, timeout: int = 240) -> tuple[str, dict]:
    """(the reply, what OpenRouter says it billed). `usage.include` asks OpenRouter to put the cost
    in the reply, so the caller can count real money against its caps instead of an estimate."""
    body = {"model": MODEL, "messages": messages, "usage": {"include": True}}
    post = POST or _post_openrouter
    last = None
    for attempt in range(3):
        try:
            resp = post(body, timeout)
            content = (resp.get("choices") or [{}])[0].get("message", {}).get("content")
            if content:
                return content, dict(resp.get("usage") or {})
            last = f"empty response: {json.dumps(resp)[:200]}"
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.read().decode()[:200]}"
        except KeyError as e:
            raise RuntimeError(f"OpenRouter key missing: set {e.args[0]}") from None
        except Exception as e:  # noqa: BLE001
            last = str(e)[:200]
    raise RuntimeError(f"OpenRouter failed after 3 tries: {last}")


def _openrouter(messages: list, timeout: int = 240) -> str:
    return _chat(messages, timeout)[0]


def billed(usage: dict) -> float:
    """Dollars OpenRouter reported for one call, 0.0 when the reply did not say."""
    try:
        return max(0.0, float((usage or {}).get("cost") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def transcribe_bytes(audio: bytes, fmt: str = "mp3", agent_name: str | None = None) -> tuple[str, dict]:
    """Audio in memory to the diarized transcript with delivery notes. Returns (text, usage)."""
    b64 = base64.b64encode(audio).decode()
    hint = f" (our agent is named {agent_name})" if agent_name else ""
    prompt = TRANSCRIBE_PROMPT.replace("{agent_hint}", hint)
    return _chat([{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "input_audio", "input_audio": {"data": b64, "format": fmt or "mp3"}},
    ]}])


def transcribe_audio(mp3: Path, agent_name: str | None = None) -> str:
    return transcribe_bytes(mp3.read_bytes(), "mp3", agent_name)[0]


def classify_text(transcript: str) -> tuple[dict, dict]:
    """The triage of one transcript (call type, pipeline, worth grading). Returns (fields, usage).
    A reply with no JSON in it is a call nobody can grade, never a guessed classification."""
    raw, usage = _chat([{"role": "user", "content": CLASSIFY_PROMPT + transcript[:24000]}], timeout=90)
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        return (json.loads(m.group(0)) if m else {"call_type": "unknown", "worth_grading": False,
                                                   "summary": raw[:200]}), usage
    except ValueError:
        return {"call_type": "unknown", "worth_grading": False, "summary": raw[:200]}, usage


def classify(transcript: str) -> dict:
    return classify_text(transcript)[0]


# ------------------------------------------------------------------ recordings from a link
# The first bytes of the audio formats a dialer hands out. Anything else (a login page, an error
# page, an empty body) is not a recording, whatever the link's status code said.
_MAGIC = ((b"ID3", "mp3"), (b"RIFF", "wav"), (b"OggS", "ogg"), (b"fLaC", "flac"))
_LOGIN_WORDS = ("login", "signin", "sign-in", "sign_in", "auth", "sso")
UA = "Mozilla/5.0 (compatible; siftstack-call-coaching)"


def audio_format(head: bytes) -> str:
    """mp3, wav, ogg, flac or m4a from a file's first bytes; "" when they are not audio."""
    h = bytes(head or b"")[:16]
    for magic, fmt in _MAGIC:
        if h.startswith(magic):
            return fmt
    if len(h) >= 2 and h[0] == 0xFF and (h[1] & 0xE0) == 0xE0:
        return "mp3"                      # an MP3 frame with no ID3 tag in front of it
    if len(h) >= 8 and h[4:8] == b"ftyp":
        return "m4a"
    return ""


def fetch_recording(url: str, max_bytes: int = 40_000_000, timeout: int = 60, probe: bool = False,
                    opener=None) -> dict:
    """Read one recording from its link. Returns {"ok", "code", "why", "host", "fmt", "size", "audio"}.

    code: ok | login | gone | not_audio | too_big | bad_link | error. A link that answers with a
    login page or a 401/403 is "login": the brain holds no dialer login and does not guess one.
    probe=True reads only the first 64 KB, enough to prove the link opens and holds audio."""
    host = urllib.parse.urlsplit(str(url or "")).hostname or ""
    out = {"ok": False, "code": "error", "why": "", "host": host, "fmt": "", "size": 0, "audio": b""}
    if urllib.parse.urlsplit(str(url or "")).scheme not in ("http", "https") or not host:
        return dict(out, code="bad_link", why="the recording link is not a web address")
    open_ = opener or urllib.request.urlopen
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "audio/*,*/*"})
        with open_(req, timeout=timeout) as r:
            final = str(getattr(r, "geturl", lambda: url)() or url).lower()
            ctype = str((getattr(r, "headers", None) or {}).get("Content-Type") or "").lower()
            limit = 65536 if probe else max_bytes + 1
            data = r.read(limit)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 407):
            return dict(out, code="login", why="the recording link needs a login the brain does not have (%s answered %d)"
                        % (host, e.code))
        if e.code in (404, 410):
            return dict(out, code="gone", why="the recording is gone (%s answered %d)" % (host, e.code))
        return dict(out, why="%s answered %d" % (host, e.code))
    except Exception as e:  # noqa: BLE001  (a timeout or a dropped connection: say it, do not retry blind)
        return dict(out, why="could not open the recording link on %s (%s)" % (host, str(e)[:80]))
    fmt = audio_format(data)
    path = urllib.parse.urlsplit(final).path
    # Audio is audio whatever the address says. Only a reply that is NOT audio is read for signs of
    # a login wall: a web page, or a redirect that landed on a sign-in address.
    if not fmt and ("html" in ctype or bytes(data[:64]).lstrip()[:1] == b"<" or any(w in path for w in _LOGIN_WORDS)):
        return dict(out, code="login", why="the recording link opened a page, not audio: it needs a login the brain "
                                            "does not have (%s)" % host)
    if not fmt:
        return dict(out, code="not_audio", why="the recording link on %s did not return audio" % host)
    if not probe and len(data) > max_bytes:
        return dict(out, code="too_big", fmt=fmt, size=len(data),
                    why="the recording is larger than %d MB, too large to send in one piece" % (max_bytes // 1_000_000))
    return dict(out, ok=True, code="ok", fmt=fmt, size=len(data), audio=b"" if probe else bytes(data))


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
