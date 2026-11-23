"""walkthrough_video: a walkthrough video in, the one repair number out, the video kept for buyers.

The lesson (comping, 10-03): film the walk in parts, carry the conservative number until quotes come,
and send the same videos to buyers. A phone video is too big to read like a photo and too long to
watch on every turn, so this workflow does what the walker would do with a notepad:

  * THE NARRATION IS TRANSCRIBED (an audio model through OpenRouter, the model the call coaching
    already uses), in chunks of AGENTS_VIDEO_CHUNK_MIN minutes so a long walk neither times out nor
    drifts. Each chunk's stated times are scaled onto its real length, as the playbook creator's
    transcriber does, because an audio model's clock runs long. It returns what was said, each room
    the walker entered, and each condition said aloud ("the roof is 20 years old"), with its time.
  * FRAMES ARE PULLED where they tell us something: two seconds after each spoken room change, plus
    one every AGENTS_VIDEO_FRAME_EVERY_S seconds so a silent stretch is still seen. Claude Haiku 5.5
    reads each frame for the room and its condition, with thinking off (voice.py's call: describing
    a kitchen needs no reasoning, and Haiku thinks by default).
  * THE NUMBER IS UNDERWRITE'S. The frames and the spoken findings become the walk's photo_notes,
    the same thing an employee writes from photos, and underwrite builds the rehab from that walk.
    This module has no rehab math of its own, so a video and a stack of photos of the same house
    cannot price differently. `plan` picks one of the engine's own plans (cosmetic, mid, gut); empty
    is the engine's one plan, said out loud by underwrite.
  * THE VIDEO IS KEPT FOR BUYERS with a short room-by-room index (time, room, what it shows) built
    from what the frames show and never from the narration: a walker says the contract price aloud
    as easily as the roof's age, and the index is what dispo sends. A dollar figure said on the video
    is named with its time, so someone checks it before the video itself goes to a buyer.
  * MONEY ONLY ON A LIVE BOX, INSIDE THE CAPS, AND ASKED FIRST WHEN LONG. Transcription and frame
    reading are paid. Dry, the run does the free steps (fetch, measure, keep the video, pull frames
    into the employee's workspace so they can be read like photos) and says what reading would cost.
    AGENTS_VIDEO_DRY_PAID=1 lets a dry box read too, the way a Slack comp ask buys Zillow data there.
    A video longer than AGENTS_VIDEO_ASK_MIN minutes, or an estimate over AGENTS_VIDEO_ASK_USD, is
    not read until the thread has the estimate and someone said go. Minutes transcribed and frames
    read are capped per ask (AGENTS_VIDEO_MAX_MIN, AGENTS_VIDEO_MAX_FRAMES).

The video arrives as a Slack file (agents/attachments.py takes mp4, mov and m4v up to
AGENTS_MAX_VIDEO_MB) or a Google Drive or Dropbox share link for anything larger. It is downloaded
into a work folder outside the volume (AGENTS_VIDEO_WORK_DIR), because one phone video can be bigger
than the brain's whole volume and the database lives there, and it is reused for a day so the go
after an estimate does not download it twice. ffmpeg is in the brain's image. Nothing is written to
DataSift, and nothing is sent to anyone.
"""
from __future__ import annotations

import base64
import csv
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import attachments, config, store
import model_registry
from . import quick_screen as qs

log = logging.getLogger(__name__)
NAME = "walkthrough_video"

# Stand-ins the selftest sets, so no test can reach ffmpeg, OpenRouter, Claude or a download. None = the real one.
PROBE = None        # (path) -> {"seconds": float, "video": bool, "audio": bool}
AUDIO = None        # (path, start_s, length_s) -> mp3 bytes
FRAME = None        # (path, at_s, out_path) -> None (writes the jpeg)
TRANSCRIBE = None   # (mp3 bytes, prompt) -> (text, usage dict with "cost")
VISION = None       # (jpeg bytes, prompt) -> (text, input tokens, output tokens)
SAVE = None         # (file record, work folder) -> dict shaped like attachments.save_video

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
SCALE = "scale='min(1024,iw)':'min(1024,ih)':force_original_aspect_ratio=decrease"
CONDITIONS = ("updated", "dated", "worn", "damaged", "gutted", "unfinished", "unclear")
GO_WORDS = {"yes", "y", "go", "true", "1", "ok", "okay", "approved", "sure", "read it", "read them"}
PLANS = {"cosmetic": "cosmetic", "light": "cosmetic", "mid": "mid", "medium": "mid", "mid reno": "mid",
         "gut": "gut_t2", "full gut": "gut_t2", "gut t2": "gut_t2", "gut_t2": "gut_t2", "full gut t2": "gut_t2",
         "gut t3": "gut_t3", "gut_t3": "gut_t3", "full gut t3": "gut_t3"}
NO_VIDEO = ("there is no walkthrough video on this thread. Post the video here (mp4, mov or m4v, up to %d MB), or "
            "paste a Google Drive or Dropbox share link for anything larger")

PROMPT = """This is the narration of a walkthrough video of a house a real estate investor may buy. The walker
films room by room and says what they see. Write EXACTLY these three sections and nothing else.

## TRANSCRIPT
Everything said, with a [M:SS] time at the start of each spoken segment (every 5 to 15 seconds).

## ROOMS
One line each time the walker enters a new room or area: [M:SS] the room's name
(kitchen, living room, primary bedroom, bedroom 2, hall bath, primary bath, laundry, basement,
crawlspace, attic, garage, roof, front exterior, back yard, and so on).

## FINDINGS
One line for each condition the walker states (an age, damage, what works or does not, what needs
replacing, a repair, a quote), in the walker's own words:
[M:SS] room | what was said
Write only what was said; add nothing of your own. If nothing was said about condition, write: none"""

VISION_PROMPT = """A frame from a walkthrough video of a house a real estate investor may buy.
The walker said they were in: %s. Around this moment they said: "%s"
Answer with JSON only, no other text:
{"room": "the room or area this frame shows (the walker's name for it when it matches)",
 "condition": "one of updated, dated, worn, damaged, gutted, unfinished, unclear",
 "shows": "one short sentence: what the frame shows about the condition",
 "findings": ["short repair findings you can actually see, at most 4"],
 "usable": true}
Set usable to false when the frame is blurry or dark, or shows only a floor, a ceiling or a person.
Describe only what is visible. No prices, no guesses about what is behind the walls."""

# A clock in the narration's own notation, with or without hours.
_TS = re.compile(r"\[(?:(\d{1,2}):)?(\d{1,3}):(\d{2})\]")
_MONEY = re.compile(r"\$\s?\d|\b\d[\d,.]*\s?(?:k|grand|thousand|dollars?|bucks)\b", re.I)
_PRICE_WORDS = re.compile(r"\b(?:price[ds]?|pricing|offer\w*|contract\w*|asking|payoff|quoted?)\b", re.I)
_PRIVATE = re.compile(r"\b(?:price[ds]?|pricing|offer\w*|contract\w*|asking|payoff|pay(?:s|ing)?|paid|owe[sd]?|owing|"
                      r"equity|profit|fee|assign\w*|wholesal\w*|mortgage|liens?|seller\w*|owner\w*|tenant\w*|landlord|"
                      r"wife|husband|son|daughter|mom|dad|mother|father|divorc\w*|estate|probate|foreclos\w*|auction|"
                      r"died|passed away|behind on|bankrupt\w*)\b", re.I)


class VideoError(RuntimeError):
    """ffmpeg or a video could not do what was asked, in plain words."""


# ------------------------------------------------------------------------------- settings

def settings() -> dict:
    s = qs.setting
    return {"max_parts": int(s("AGENTS_VIDEO_MAX_PARTS", 8)), "max_min": float(s("AGENTS_VIDEO_MAX_MIN", 30.0)),
            "max_frames": int(s("AGENTS_VIDEO_MAX_FRAMES", 40)), "every": float(s("AGENTS_VIDEO_FRAME_EVERY_S", 30.0)),
            "ask_min": float(s("AGENTS_VIDEO_ASK_MIN", 15.0)), "ask_usd": float(s("AGENTS_VIDEO_ASK_USD", 0.25)),
            "run_cap": float(s("AGENTS_VIDEO_RUN_CAP_USD", 1.0)), "day_cap": float(s("AGENTS_VIDEO_DAILY_USD", 3.0)),
            "per_min": float(s("AGENTS_VIDEO_AUDIO_USD_PER_MIN", 0.003)), "frame_usd": float(s("AGENTS_VIDEO_FRAME_USD", 0.0005)),
            "chunk_min": max(1.0, float(s("AGENTS_VIDEO_CHUNK_MIN", 10.0))),
            "keep_mb": float(s("AGENTS_VIDEO_KEEP_MB", 100.0)), "keep_total_mb": float(s("AGENTS_VIDEO_KEEP_TOTAL_MB", 300.0)),
            "work_dir": str(s("AGENTS_VIDEO_WORK_DIR", str(Path(tempfile.gettempdir()) / "siftstack-video"))),
            "work_hours": float(s("AGENTS_VIDEO_WORK_HOURS", 24.0)), "dry_paid": bool(s("AGENTS_VIDEO_DRY_PAID", False)),
            "audio_model": str(s("AGENTS_VIDEO_AUDIO_MODEL", model_registry.model("audio_to_text"))),
            "vision_model": str(s("AGENTS_VIDEO_VISION_MODEL", model_registry.model("reading_images"))),
            "workers": max(1, int(s("AGENTS_VIDEO_VISION_WORKERS", 4))), "private": bool(s("AGENTS_VIDEO_PRIVATE_ROUTING", True))}


def paid_ok(st: dict) -> bool:
    return config.live() or st["dry_paid"]


def _go(inputs: dict) -> bool:
    return str(inputs.get("go") or "").strip().lower() in GO_WORDS


# ------------------------------------------------------------------------------- small helpers

def clock(seconds: float) -> str:
    s = int(round(max(0.0, seconds)))
    h, rest = divmod(s, 3600)
    return ("%d:%02d:%02d" % (h, rest // 60, rest % 60)) if h else ("%d:%02d" % (rest // 60, rest % 60))


def minutes_text(seconds: float) -> str:
    m = seconds / 60.0
    return "%d min" % max(1, round(m)) if m >= 1 else "%d sec" % max(1, round(seconds))


def _room(text) -> str:
    t = re.sub(r"\s+", " ", str(text or "")).strip().strip(".:;,-").strip()
    return (t[:1].upper() + t[1:])[:40] if t and t.lower() not in ("none", "not said", "unknown", "n/a") else ""


def _room_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _sentence(text, keep: int = 160) -> str:
    t = re.sub(r"\s+", " ", str(text or "")).strip()
    return t[:keep].rstrip(" ,;") if t else ""


def _uniq(items: list[str]) -> list[str]:
    """Each thing once: two frames of the same kitchen often read the same."""
    seen, out = set(), []
    for x in items:
        k = re.sub(r"[^a-z0-9]+", " ", x.lower()).strip()
        if k and k not in seen:
            seen.add(k)
            out.append(x)
    return out


def buyer_safe(text: str) -> bool:
    """A line a buyer may read: no dollar figure, nothing about price or the deal, nothing about the
    seller's people or situation, no phone number."""
    return bool(text) and not (_MONEY.search(text) or _PRIVATE.search(text) or qs._PHONE.search(text))


def _money_said(text: str) -> bool:
    return bool(_MONEY.search(text or "") or (_PRICE_WORDS.search(text or "") and re.search(r"\d", text or "")))


def _part_name(p: dict, many: bool) -> str:
    return ("video %d" % p["n"]) if many else "the video"


def _at(p: dict, t: float, many: bool) -> str:
    return ("video %d at %s" % (p["n"], clock(t))) if many else clock(t)


# ------------------------------------------------------------------------------- ffmpeg

def _ff(cmd: list, timeout: int) -> subprocess.CompletedProcess:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise VideoError("ffmpeg is not installed on this box") from None
    except subprocess.TimeoutExpired:
        raise VideoError("ffmpeg took longer than %d seconds" % timeout) from None
    if r.returncode != 0:
        log.warning("%s failed: %s", cmd[0], (r.stderr or "").strip()[-400:])
        raise VideoError("ffmpeg could not read it")
    return r


def probe(path: Path) -> dict:
    if PROBE is not None:
        return dict(PROBE(path))
    r = _ff(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type", "-of", "json", str(path)], 60)
    try:
        d = json.loads(r.stdout or "{}")
    except ValueError:
        raise VideoError("ffprobe gave no answer") from None
    types = {s.get("codec_type") for s in d.get("streams") or []}
    return {"seconds": float((d.get("format") or {}).get("duration") or 0), "video": "video" in types, "audio": "audio" in types}


def audio(path: Path, start: float, length: float) -> bytes:
    """Mono speech-quality mp3 (16 kHz, 32 kbps) of one stretch: ten minutes is about 2.4 MB."""
    if AUDIO is not None:
        return AUDIO(path, start, length)
    with tempfile.TemporaryDirectory() as td:
        mp3 = Path(td) / "chunk.mp3"
        _ff(["ffmpeg", "-y", "-v", "error", "-ss", "%.2f" % start, "-t", "%.2f" % length, "-i", str(path), "-vn",
             "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "32k", str(mp3)], 300)
        return mp3.read_bytes()


def frame(path: Path, at: float, out: Path) -> None:
    """One frame, fitted inside 1024 pixels and never enlarged; a frame past the end writes nothing."""
    if FRAME is not None:
        FRAME(path, at, out)
    else:
        _ff(["ffmpeg", "-y", "-v", "error", "-ss", "%.2f" % at, "-i", str(path), "-frames:v", "1", "-vf", SCALE,
             "-q:v", "4", str(out)], 60)
    if not (out.is_file() and out.stat().st_size > 0):
        raise VideoError("no frame at %s" % clock(at))


# ------------------------------------------------------------------------------- the paid calls

def _openrouter(mp3: bytes, st: dict) -> tuple[str, dict]:
    """(the narration's three sections, what OpenRouter says it billed). Copied in small from the call
    coaching transcriber: usage.include puts the real cost in the reply, so the caps count money,
    and the walker's voice and the seller's house are not offered to a host that keeps data."""
    if TRANSCRIBE is not None:
        return TRANSCRIBE(mp3, PROMPT)
    key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    body = {"model": st["audio_model"], "usage": {"include": True}, "messages": [{"role": "user", "content": [
        {"type": "text", "text": PROMPT},
        {"type": "input_audio", "input_audio": {"data": base64.b64encode(mp3).decode(), "format": "mp3"}}]}]}
    if st["private"]:
        body["provider"] = {"data_collection": "deny"}
    req = urllib.request.Request(OPENROUTER_URL, data=json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer %s" % key, "Content-Type": "application/json"})
    last = ""
    for _ in range(2):
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                resp = json.loads(r.read())
            content = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content")
            if isinstance(content, list):
                content = "".join(str(c.get("text") or "") for c in content if isinstance(c, dict))
            if content:
                return str(content), dict(resp.get("usage") or {})
            last = "an empty reply"
        except urllib.error.HTTPError as exc:
            last = "HTTP %s" % exc.code
        except Exception as exc:  # noqa: BLE001
            last = type(exc).__name__
    raise VideoError("the transcription failed (%s)" % last)


def _billed(usage: dict) -> float:
    try:
        return max(0.0, float((usage or {}).get("cost") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _haiku(jpeg: bytes, prompt: str, model: str) -> tuple[str, int, int]:
    """One frame read, the way voice.py asks Haiku: thinking off, effort low, and a model that refuses
    "disabled" asked once more without it. A cut-off or refused answer is no answer."""
    if VISION is not None:
        return VISION(jpeg, prompt)
    import anthropic
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    base = dict(model=model, output_config={"effort": "low"}, messages=[{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                     "data": base64.standard_b64encode(jpeg).decode("utf-8")}},
        {"type": "text", "text": prompt}]}])
    try:
        msg = client.messages.create(max_tokens=600, thinking={"type": "disabled"}, **base)
    except anthropic.BadRequestError:
        msg = client.messages.create(max_tokens=3000, **base)
    u = getattr(msg, "usage", None)
    tin, tout = int(getattr(u, "input_tokens", 0) or 0), int(getattr(u, "output_tokens", 0) or 0)
    if getattr(msg, "stop_reason", "") in ("refusal", "max_tokens"):
        return "", tin, tout
    return "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", "") == "text").strip(), tin, tout


def _vision_usd(model: str, tin: int, tout: int) -> float:
    from .. import voice
    p_in, p_out = voice.PRICES.get(model, voice.DEFAULT_PRICE)
    return (tin * p_in + tout * p_out) / 1e6


def keys_missing(st: dict) -> tuple[str, str]:
    """(why the narration cannot be read, why the frames cannot be read); "" when they can."""
    t = "" if TRANSCRIBE is not None or (os.environ.get("OPENROUTER_API_KEY") or "").strip() else \
        "OPENROUTER_API_KEY is not set on the brain, so the narration cannot be transcribed"
    v = "" if VISION is not None or config.ANTHROPIC_API_KEY else \
        "ANTHROPIC_API_KEY is not set on the brain, so the frames cannot be read"
    return t, v


class Spend:
    """The per-ask cap and the brain's daily cap (quick_screen.Budget), plus a day budget for video
    alone (AGENTS_VIDEO_DAILY_USD), so a morning of long walkthroughs cannot use the whole day's cap."""

    def __init__(self, task: dict, st: dict):
        self.budget = qs.Budget(task.get("id") or 0, st["run_cap"])
        self.day_cap, self.key, self.spent = st["day_cap"], "video:usd:%s" % store.today(), 0.0

    @property
    def skipped(self) -> list[str]:
        return self.budget.skipped

    def used_today(self) -> float:
        try:
            return float(store.kv_get(self.key) or 0)
        except ValueError:
            return 0.0

    def allows(self, usd: float, what: str) -> bool:
        if self.used_today() + usd > self.day_cap:
            self.budget.skipped.append("%s skipped: about $%.2f would pass today's video budget of $%.2f (AGENTS_VIDEO_DAILY_USD)"
                                       % (what, usd, self.day_cap))
            return False
        return self.budget.allows(usd, what)

    def charge(self, usd: float, what: str) -> None:
        if usd <= 0:
            return
        self.budget.charge(usd, "walkthrough video: %s" % what)
        store.kv_set(self.key, "%.6f" % (self.used_today() + usd))
        self.spent += usd


# ------------------------------------------------------------------------------- reading the narration

def parse_narration(text: str, real_s: float) -> dict:
    """{"said": [(t, text)], "rooms": [(t, room)], "findings": [(t, room, text)]} from the model's three
    sections. Times are scaled onto the chunk's real length when the stated clock ran past it (an
    audio model's clock runs long on a long file) and held inside the chunk."""
    section, said, rooms, findings, stamps = "transcript", [], [], [], []
    for raw in (text or "").splitlines():
        line = raw.strip()
        # A heading however the model wrote it: "## ROOMS", "**Rooms**", "ROOMS:".
        word = re.sub(r"[^A-Za-z]", "", line).upper()
        if word in ("TRANSCRIPT", "ROOMS", "FINDINGS") and not _TS.search(line):
            section = word.lower()
            continue
        marks = [m for m in _TS.finditer(line) if int(m.group(3)) < 60]
        if not marks:
            continue
        for i, m in enumerate(marks):
            t = int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            body = line[m.end(): marks[i + 1].start() if i + 1 < len(marks) else len(line)].strip(" -:")
            stamps.append(t)
            if section == "rooms":
                if _room(body):
                    rooms.append((t, _room(body)))
            elif section == "findings":
                if "|" in body:
                    room, what = body.split("|", 1)
                else:
                    room, what = "", body
                what = _sentence(what, 240)
                if what and what.lower() != "none":
                    findings.append((t, _room(room), what))
            elif body:
                said.append((t, _sentence(body, 400)))
            if section != "transcript":
                break          # a room or finding line carries one time
    stated = max(stamps or [0])
    scale = (real_s / stated) if real_s > 0 and stated > real_s + 2 else 1.0

    def fix(t: float) -> float:
        return round(min(max(0.0, t * scale), max(0.0, real_s)), 1)

    return {"said": [(fix(t), x) for t, x in said], "rooms": [(fix(t), x) for t, x in rooms],
            "findings": [(fix(t), r, x) for t, r, x in findings], "scaled": scale != 1.0}


def transcribe_part(p: dict, st: dict, spend: Spend, problems: list) -> None:
    """The narration of one video, chunk by chunk, inside the caps. Fills p["said"], p["rooms"],
    p["findings"] and p["heard_s"] (seconds actually transcribed)."""
    p.update(said=[], rooms=[], findings=[], heard_s=0.0)
    if not p.get("audio"):
        problems.append("%s has no sound, so there is no narration to read" % p["label"])
        return
    chunk, t0 = st["chunk_min"] * 60.0, 0.0
    while t0 < p["read_s"] - 0.5:
        length = min(chunk, p["read_s"] - t0)
        est = length / 60.0 * st["per_min"]
        if not spend.allows(est, "narration of %s from %s" % (p["label"], clock(t0))):
            return
        try:
            mp3 = audio(p["path"], t0, length)
            text, usage = _openrouter(mp3, st)
        except Exception as exc:  # noqa: BLE001  (ffmpeg, the network, the model: said once, the frames still read)
            problems.append("the narration of %s from %s could not be read: %s" % (p["label"], clock(t0), str(exc)[:160]))
            return
        spend.charge(_billed(usage) or est, "transcribed %s of %s" % (minutes_text(length), p["label"]))
        got = parse_narration(text, length)
        p["said"] += [(t0 + t, x) for t, x in got["said"]]
        p["rooms"] += [(t0 + t, x) for t, x in got["rooms"]]
        p["findings"] += [(t0 + t, r, x) for t, r, x in got["findings"]]
        p["heard_s"] += length
        t0 += length


# ------------------------------------------------------------------------------- frames

def _steady(read_s: float, every: float) -> list[float]:
    times, t = [], every / 2.0
    while t < read_s - 0.5:
        times.append(round(t, 1))
        t += every
    return times or ([round(read_s / 2.0, 1)] if read_s > 1 else [])


def _thin(items: list, k: int) -> list:
    """k items spread evenly across the list, first and last kept."""
    if k <= 0:
        return []
    if len(items) <= k:
        return list(items)
    if k == 1:
        return [items[len(items) // 2]]
    return [items[round(i * (len(items) - 1) / (k - 1))] for i in range(k)]


def plan_frames(parts: list[dict], st: dict, rooms_known: bool) -> list[dict]:
    """Where frames are pulled: two seconds into each spoken room, then the steady ones that are not
    within eight seconds of a room frame. Room frames come first under the cap, then steady ones
    spread across every video, so a long first video cannot use the whole cap."""
    room_frames, steady = [], []
    for p in parts:
        if p["read_s"] <= 0.5:
            continue
        marks = sorted(p.get("rooms") or []) if rooms_known else []
        for t, room in marks:
            room_frames.append({"part": p["n"], "at": round(min(t + 2.0, p["read_s"] - 0.5), 1), "why": "room", "hint": room})
        taken = [f["at"] for f in room_frames if f["part"] == p["n"]]
        steady += [{"part": p["n"], "at": t, "why": "steady", "hint": ""}
                   for t in _steady(p["read_s"], st["every"]) if all(abs(t - a) > 8.0 for a in taken)]
    cap = st["max_frames"]
    picked = _thin(room_frames, cap)
    picked += _thin(steady, cap - len(picked))
    picked.sort(key=lambda f: (f["part"], f["at"]))
    by = {p["n"]: sorted(p.get("rooms") or []) for p in parts}
    for f in picked:
        if not f["hint"]:
            before = [r for t, r in by.get(f["part"], []) if t <= f["at"] + 0.5]
            f["hint"] = before[-1] if before else ""
    return picked


def pull_frames(parts: list[dict], plan: list[dict], into: Path, problems: list) -> list[dict]:
    """The planned frames written to disk (free). A frame that cannot be pulled is left out and counted."""
    by = {p["n"]: p for p in parts}
    into.mkdir(parents=True, exist_ok=True)
    out, missed, names = [], 0, set()
    for f in plan:
        p = by[f["part"]]
        m, s = divmod(int(f["at"]), 60)
        name = "v%d-%dm%02ds%s.jpg" % (p["n"], m, s, ("-" + attachments.safe(f["hint"].lower(), 20)) if f["hint"] else "")
        while name in names:            # two frames in the same second keep two files
            name = name[:-4] + "-2.jpg"
        names.add(name)
        path = into / name
        try:
            frame(p["path"], f["at"], path)
        except VideoError:
            missed += 1
            continue
        out.append(dict(f, name=name, path=path))
    if missed:
        problems.append("%d frame%s could not be pulled from the video" % (missed, "" if missed == 1 else "s"))
    return out


def _near(p: dict, t: float) -> str:
    """What the walker said around one moment, for the frame reader and the evidence column."""
    words = [x for s, x in p.get("said") or [] if t - 12 <= s <= t + 8]
    return _sentence(" ".join(words), 240)


def parse_seen(text: str) -> dict | None:
    i, j = (text or "").find("{"), (text or "").rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        d = json.loads(text[i:j + 1])
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    cond = str(d.get("condition") or "unclear").strip().lower()
    found = d.get("findings") if isinstance(d.get("findings"), list) else []
    return {"room": _room(d.get("room")), "condition": cond if cond in CONDITIONS else "unclear",
            "shows": _sentence(d.get("shows")), "findings": [_sentence(x, 90) for x in found if _sentence(x, 90)][:4],
            "usable": d.get("usable") is not False}


def read_frames(parts: list[dict], frames: list[dict], st: dict, spend: Spend, problems: list) -> None:
    """Each frame read by the vision model, a few at a time, each batch asking the caps first and paying
    after. Fills f["seen"] (None when the answer could not be read) and f["read"]."""
    by = {p["n"]: p for p in parts}
    jobs = []
    for f in frames:
        hint = f["hint"] or "not said"
        jobs.append((f, VISION_PROMPT % (hint, _near(by[f["part"]], f["at"]).replace('"', "'") or "nothing")))

    def one(job):
        f, prompt = job
        try:
            text, tin, tout = _haiku(Path(f["path"]).read_bytes(), prompt, st["vision_model"])
        except Exception as exc:  # noqa: BLE001  (one frame never stops the rest)
            return f, None, 0.0, type(exc).__name__
        return f, parse_seen(text), _vision_usd(st["vision_model"], tin, tout), ""

    unread = 0
    with ThreadPoolExecutor(max_workers=st["workers"]) as pool:
        for i in range(0, len(jobs), st["workers"]):
            batch = jobs[i:i + st["workers"]]
            if not spend.allows(len(batch) * st["frame_usd"], "reading %d frame%s" % (len(batch), "" if len(batch) == 1 else "s")):
                break
            cost = 0.0
            for f, seen, usd, err in pool.map(one, batch):
                cost += usd
                f["read"], f["seen"] = True, seen
                if seen is None:
                    unread += 1
                    if err:
                        log.info("frame %s not read: %s", f["name"], err)
            spend.charge(cost, "read %d frame%s" % (len(batch), "" if len(batch) == 1 else "s"))
    if unread:
        problems.append("%d frame%s came back unreadable and %s left out" % (unread, "" if unread == 1 else "s",
                                                                             "was" if unread == 1 else "were"))


# ------------------------------------------------------------------------------- the walk and the index

def build_walk(parts: list[dict], frames: list[dict], many: bool) -> tuple[dict, list[dict], list[dict]]:
    """(walk, frames it used, spoken findings) in the shape an employee writes from photos: one
    photo_note per usable frame and one per thing said about condition. A said dollar figure is a
    note marked not priced, never a priced flag: the engine already prices that line, and a figure
    heard on a video is not a signed bid."""
    notes, used, spoken = [], [], []
    by = {p["n"]: p for p in parts}
    for f in frames:
        seen = f.get("seen") or {}
        if not (f.get("read") and seen and seen.get("usable")):
            continue
        p = by[f["part"]]
        room = seen.get("room") or f["hint"]
        finding = "; ".join([x for x in [seen.get("shows")] + list(seen.get("findings") or []) if x])
        notes.append({"file": f["name"], "room": room, "finding": finding, "condition": seen.get("condition") or "unclear",
                      "scope_line": "", "quote": _near(p, f["at"])})
        used.append(dict(f, room=room))
    for p in parts:
        for t, room, what in p.get("findings") or []:
            money = _money_said(what)
            spoken.append({"part": p["n"], "at": t, "room": room, "said": what, "money": money})
            notes.append({"file": "%s (said on the walk)" % _at(p, t, many), "room": room, "finding": what,
                          "condition": "said on the walk, a dollar figure: not priced" if money else "said on the walk",
                          "scope_line": "", "quote": what})
    heard = sum(p.get("heard_s") or 0 for p in parts)
    walk = {"photo_notes": notes,
            "note": "From the walkthrough video: %s of narration, %d frame%s read, %d thing%s said about condition." % (
                minutes_text(heard) if heard else "no", len(used), "" if len(used) == 1 else "s",
                len(spoken), "" if len(spoken) == 1 else "s")}
    return walk, used, spoken


def plan_walk(walk: dict, plan_text: str, subj: dict) -> tuple[dict, str]:
    """(walk, problem). `plan` picks one of the engine's own plans for this house (its own layout, as
    underwrite holds it), or carries an inline single_scenario; the definitions are the engine's, so the
    plan here and the plan underwrite prices are one thing."""
    import post_walkthrough as pw
    from .underwrite import _pw_subject
    text = (plan_text or "").strip()
    if not text:
        return walk, ""
    if text.startswith("{"):
        try:
            got = json.loads(text)
        except ValueError:
            return walk, "the plan JSON could not be read, so the engine's one plan is used"
        return (dict(walk, single_scenario=got), "") if isinstance(got, dict) else (walk, "the plan JSON is not an object")
    key = PLANS.get(re.sub(r"[\s_-]+", " ", text.lower()).strip()) or PLANS.get(text.lower())
    if not key:
        return walk, ("the plan %r is not one the engine knows (cosmetic, mid, gut or gut t3), so the engine's one "
                      "plan is used" % text[:40])
    pinned, _ = qs.pinned_walk(subj, walk)
    d = next(x for x in pw.scenario_defs(_pw_subject(subj), pinned) if x["key"] == key)
    plan = {"key": d["key"], "label": d["label"], "tier": d["tier"], "scope": d["scope"], "gut": d["gut"],
            "beds": d["beds"], "baths": d["baths"], "drop": list(d["drop"])}
    return dict(walk, single_scenario=plan), ""


def money_moments(parts: list[dict], many: bool) -> list[str]:
    out = []
    for p in parts:
        for t, x in p.get("said") or []:
            if _money_said(x):
                out.append(_at(p, t, many))
    return out


def index_entries(parts: list[dict], used: list[dict], spoken: list[dict]) -> list[dict]:
    """What a buyer may read, in time order: each usable frame's description, and each condition said
    aloud that passes buyer_safe. The narration itself never goes in."""
    rows = []
    for f in used:
        seen = f.get("seen") or {}
        what = "; ".join([x for x in [seen.get("shows")] + list(seen.get("findings") or [])[:2] if x and buyer_safe(x)])
        if what:
            rows.append({"part": f["part"], "at": f["at"], "room": f["room"], "source": "frame", "what": what, "frame": f["name"]})
    for s in spoken:
        if not s["money"] and buyer_safe(s["said"]):
            rows.append({"part": s["part"], "at": s["at"], "room": s["room"], "source": "said", "what": s["said"], "frame": ""})
    rows.sort(key=lambda r: (r["part"], r["at"]))
    return rows


def write_index(folder: Path, stem: str, street: str, parts: list[dict], rows: list[dict], many: bool, task: dict) -> list[str]:
    """The buyer index (markdown to read, CSV to sort). One line per room visit: a run of entries in
    the same room is one visit, with at most two things it shows and two things said."""
    lines = ["Walkthrough index: %s" % (street or "this house"), "",
             "What each part of the walkthrough shows, for a buyer. Built from the video's frames; nothing said on "
             "the video about price, the deal or the seller is in it.", "", "Videos"]
    for p in parts:
        if p.get("link"):
            where = "shared link: %s" % p["link"]
        elif p.get("kept"):
            url = qs.link(task, p["kept"])
            where = "posted in Slack, kept with this index as %s%s" % (p["kept"], (": " + url) if url else "")
        else:
            where = "posted in the Slack thread"
        lines.append("%d. %s, %s (%s)" % (p["n"], p["name"], minutes_text(p["seconds"]), where))
    lines += ["", "Room by room"]
    if not rows:
        lines.append("Nothing in the frames could be described.")
    visits: list[dict] = []
    for r in rows:
        last = visits[-1] if visits else None
        if last and last["part"] == r["part"] and _room_key(last["room"]) == _room_key(r["room"]):
            last["rows"].append(r)
        else:
            visits.append({"part": r["part"], "at": r["at"], "room": r["room"], "rows": [r]})
    part_now = None
    for v in visits:
        if many and v["part"] != part_now:
            part_now = v["part"]
            lines += ["", "Video %d" % part_now]
        shows = _uniq([x["what"] for x in v["rows"] if x["source"] == "frame"])[:2]
        said = _uniq([x["what"] for x in v["rows"] if x["source"] == "said"])[:2]
        text = ". ".join(s.rstrip(".") for s in shows)
        if said:
            text += (". " if text else "") + "Said on the walk: " + "; ".join(s.rstrip(".") for s in said)
        lines.append("%s  %s: %s." % (clock(v["at"]), v["room"] or "Unnamed room", text.rstrip(".")))
    md, cs = stem + "_Walkthrough_Index.md", stem + "_Walkthrough_Index.csv"
    (folder / md).write_text("\n".join(lines) + "\n", encoding="utf-8")
    with open(folder / cs, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["Video", "Time", "Room", "From", "What it shows", "Frame"])
        for r in rows:
            w.writerow([r["part"], clock(r["at"]), r["room"], "frame" if r["source"] == "frame" else "said on the walk",
                        r["what"], r["frame"]])
    return [md, cs]


def write_transcript(folder: Path, stem: str, street: str, parts: list[dict], many: bool) -> str:
    """The narration for the team. Not for buyers: it holds whatever the walker said."""
    lines = ["Walkthrough narration: %s (for the team, not for buyers)" % (street or "this house")]
    for p in parts:
        if not p.get("heard_s"):
            continue
        lines += ["", ("Video %d: %s" % (p["n"], p["name"])) if many else p["name"]]
        lines += ["[%s] %s" % (clock(t), x) for t, x in p.get("said") or []]
        if p.get("rooms"):
            lines += ["", "Rooms: " + ", ".join("%s %s" % (clock(t), r) for t, r in p["rooms"])]
        if p.get("findings"):
            lines += ["", "Said about condition:"] + ["[%s] %s%s" % (clock(t), (r + ": ") if r else "", x)
                                                     for t, r, x in p["findings"]]
    name = stem + "_Walkthrough_Transcript.md"
    (folder / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return name


# ------------------------------------------------------------------------------- keeping the video

def _kept_total() -> int:
    """Bytes of walkthrough videos already kept in this workflow's run folders (the volume is shared
    with the database, and run folders are not pruned)."""
    total = 0
    for p in Path(config.RUNS_DIR).glob("%s-*/files/*" % NAME):
        if p.suffix.lower().lstrip(".") in attachments.VIDEO_EXT:
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def keep_originals(parts: list[dict], folder: Path, stem: str, st: dict) -> None:
    """A video posted in Slack is copied next to the index when it is small enough and the volume keeps
    room for the database (the volume is small and run folders are not pruned); otherwise the original
    stays in the Slack thread and the index says so. A share link is kept as the link."""
    total, mb = _kept_total(), attachments.MB
    floor = attachments.disk_floor_mb() * mb
    for p in parts:
        p.setdefault("kept", "")
        if p.get("link"):
            continue
        size = Path(p["path"]).stat().st_size
        if size > st["keep_mb"] * mb:
            p["keep_why"] = "%s is %s, more than the %d MB kept on the box, so the original stays in the Slack thread" % (
                p["name"], attachments.size_text(size), int(st["keep_mb"]))
        elif total + size > st["keep_total_mb"] * mb:
            p["keep_why"] = ("the box already keeps %s of walkthrough videos (AGENTS_VIDEO_KEEP_TOTAL_MB), so the original "
                             "of %s stays in the Slack thread" % (attachments.size_text(total), p["name"]))
        elif attachments.free_bytes(folder) - size < floor:
            p["keep_why"] = "the box's disk is too full to keep a copy of %s, so the original stays in the Slack thread" % p["name"]
        else:
            name = "%s_Walkthrough_Video_%d%s" % (stem, p["n"], Path(p["path"]).suffix.lower() or ".mp4")
            try:
                shutil.copyfile(p["path"], folder / name)
                p["kept"], total = name, total + size
            except OSError as exc:
                p["keep_why"] = "the copy of %s could not be kept (%s)" % (p["name"], type(exc).__name__)


# ------------------------------------------------------------------------------- the run

def _prune_work(root: Path, hours: float) -> None:
    if not root.is_dir():
        return
    cut = time.time() - hours * 3600
    for d in root.iterdir():
        try:
            if d.is_dir() and d.stat().st_mtime < cut:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


def choose(task: dict, inputs: dict, details: str, st: dict) -> tuple[list[dict], list[str], str]:
    """(the videos to read, notes, why none). The `video` input names one (a share link, or the file
    name of a video on the thread); empty is every video and link on the thread plus links in the ask."""
    on = attachments.videos_on_thread(task.get("slack_channel") or "", task.get("slack_thread") or "")
    pick, notes = str(inputs.get("video") or "").strip(), []
    if pick:
        chosen = attachments.links_from_text(pick)
        if not chosen:
            low = pick.lower()
            chosen = [f for f in on if (f.get("name") or "").lower() in (low, Path(low).stem)
                      or Path((f.get("name") or "").lower()).stem == low]
        if not chosen:
            return [], [], "no video named %s is on this thread, and it is not a Google Drive or Dropbox link" % pick[:60]
    else:
        chosen = list(on)
        ids = {f.get("id") for f in chosen}
        chosen += [f for f in attachments.links_from_text(details) if f["id"] not in ids]
    if any(f.get("kind") == "link" and not f.get("folder") for f in chosen):
        big = [f for f in chosen if attachments.too_big(f)]
        for f in big:
            notes.append("%s is too large to take from Slack, so the share link is read instead" % f.get("name"))
        chosen = [f for f in chosen if f not in big]
    if not chosen:
        return [], notes, NO_VIDEO % int(attachments.video_mb())
    if len(chosen) > st["max_parts"]:
        notes.append("only the first %d videos are read (AGENTS_VIDEO_MAX_PARTS caps one ask)" % st["max_parts"])
        chosen = chosen[:st["max_parts"]]
    return chosen, notes, ""


def fetch(chosen: list[dict], st: dict, problems: list) -> list[dict]:
    """Each video downloaded into the work folder (or found there from an earlier ask) and measured.
    One that cannot be fetched is said once, in one line, and remembered for the employee's prompt."""
    root = Path(st["work_dir"])
    parts = []
    for f in chosen:
        work = root / attachments.safe(str(f.get("id") or "video"), 40)
        got = (SAVE or attachments.save_video)(f, work)
        name = str(got.get("name") or f.get("name") or "the video")
        if got.get("not_video"):
            continue
        if got.get("why") or not got.get("path"):
            why = got.get("why") or "it could not be downloaded"
            problems.append("could not read %s: %s" % (name, why))
            attachments.note_failure(str(f.get("id") or ""), why)
            continue
        try:
            info = probe(Path(got["path"]))
        except VideoError as exc:
            problems.append("could not read %s: %s" % (name, exc))
            continue
        if not info.get("video") or info.get("seconds", 0) <= 0:
            problems.append("could not read %s: it holds no video ffmpeg can play" % name)
            continue
        attachments.clear_failure(str(f.get("id") or ""))
        try:
            os.utime(work, None)
        except OSError:
            pass
        link = f.get("url") if f.get("kind") == "link" else ""
        parts.append({"n": len(parts) + 1, "id": f.get("id"), "name": name, "path": str(got["path"]), "link": link,
                      "source": attachments.HOSTS.get(f.get("host") or "", "Slack"), "seconds": float(info["seconds"]),
                      "audio": bool(info.get("audio"))})
    return parts


def window(parts: list[dict], st: dict) -> None:
    """How much of each video one ask reads: in order, until AGENTS_VIDEO_MAX_MIN is used."""
    left = st["max_min"] * 60.0
    many = len(parts) > 1
    for p in parts:
        p["read_s"] = max(0.0, min(p["seconds"], left))
        left -= p["read_s"]
        p["label"] = _part_name(p, many)


def estimate(parts: list[dict], st: dict) -> dict:
    read_s = sum(p["read_s"] for p in parts)
    steady = sum(len(_steady(p["read_s"], st["every"])) for p in parts if p["read_s"] > 0.5)
    frames = min(st["max_frames"], steady + int(read_s / 60.0) + 2)
    heard = sum(p["read_s"] for p in parts if p.get("audio"))
    usd = heard / 60.0 * st["per_min"] + frames * st["frame_usd"]
    return {"seconds": sum(p["seconds"] for p in parts), "read_s": read_s, "frames": frames, "usd": round(usd, 4)}


def _subject(task: dict, inputs: dict, details: str) -> tuple[dict | None, dict, str]:
    """(subject, area, why). The house is checked before anything is downloaded or paid for: a video read
    for a house that cannot be priced is money spent on the wrong question. No address is allowed:
    the video is then read for the index and the findings, with no repair number."""
    from .underwrite import resolve_subject
    area = qs.area_inputs(inputs, details)
    if area["why"]:
        return None, area, area["why"]
    if not str(inputs.get("address") or "").strip():
        return None, area, ""
    subj, _rec, _fields, why = resolve_subject(task, inputs)
    if not subj:
        return None, area, why
    qs.apply_notes(subj, area["notes"])
    if not subj["sqft"]:
        return None, area, ("there is no square footage for this house; put it in the notes "
                            "(for example 3 bed 1 bath 1,400 sqft built 1950)")
    return subj, area, ""


def _underwrite(task: dict, inputs: dict, details: str, walk: dict, folder: Path) -> dict:
    """Underwrite on the walk the video produced, as one request: the same function a walk written from
    photos goes through. Its files are copied next to this run's, where the Slack links find them."""
    from . import underwrite as uw
    keep = {k: inputs.get(k) for k in ("address", "notes", "boundary", "streets") if inputs.get(k) not in (None, "")}
    sub = dict(task, payload=json.dumps({"inputs": dict(keep, walk=walk), "details": details}, default=str))
    try:
        ur = uw.run(sub)
    except Exception as exc:  # noqa: BLE001  (said in the result, never raised into the worker)
        log.exception("underwrite on the video's walk failed")
        return {"ok": False, "why": "underwrite failed on the video's walk (%s)" % str(exc)[:160]}
    src = Path(ur.get("run_dir") or "") / "files"
    for name in ur.get("files") or []:
        try:
            if (src / name).is_file():
                shutil.copyfile(src / name, folder / name)
        except OSError as exc:
            log.warning("could not copy %s: %s", name, exc)
    return ur


def run(task: dict) -> dict:
    from .offer_sheet import safe_name
    inputs, details = qs.inputs_of(task)
    st = settings()
    folder = qs.files_dir(task, NAME)
    out = {"ok": False, "mode": "", "run_dir": str(folder.parent), "files": [], "problems": [], "notes": [],
           "skipped": [], "parts": [], "frames_saved": 0, "frames_read": 0, "spent_usd": 0.0}
    root = Path(st["work_dir"])
    _prune_work(root, st["work_hours"])
    subj, area, why = _subject(task, inputs, details)
    if why:
        out["why"] = why
        return out
    out["street"] = (subj or {}).get("street") or ""
    stem = safe_name(out["street"] or "Walkthrough", "")
    chosen, notes, why = choose(task, inputs, details, st)
    out["notes"] = notes
    if why:
        out["why"] = why
        return out
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        out["why"] = "the work folder could not be made (%s)" % type(exc).__name__
        return out
    parts = fetch(chosen, st, out["problems"])
    if not parts:
        out["why"] = "none of the videos could be read"
        return out
    window(parts, st)
    many = len(parts) > 1
    est = estimate(parts, st)
    out.update(estimate=est, seconds=est["seconds"], read_s=est["read_s"])
    paid = paid_ok(st)

    # Asked first when long, when dear, and always when the estimate passes what one ask may spend.
    if paid and not _go(inputs) and (est["seconds"] / 60.0 > st["ask_min"] or est["usd"] > min(st["ask_usd"], st["run_cap"])):
        out.update(ok=True, mode="asked", needs_go=True, parts=[_public(p) for p in parts], run_cap=st["run_cap"])
        return out

    if not paid:
        keep_originals(parts, folder, stem, st)
        frames_tmp = root / ("frames-%d" % int(task.get("id") or 0))
        try:
            plan = plan_frames(parts, st, rooms_known=False)
            pulled = pull_frames(parts, plan, frames_tmp, out["problems"])
            saved = _frames_to_workspace(task, parts, pulled, read=False, many=many)
        finally:
            shutil.rmtree(frames_tmp, ignore_errors=True)
        # No index on a dry box: nothing has been read, and an index of times alone tells a buyer nothing.
        out["files"] = [p["kept"] for p in parts if p.get("kept")]
        out.update(ok=True, mode="dry", frames_saved=saved, parts=[_public(p) for p in parts],
                   kept_why=[p["keep_why"] for p in parts if p.get("keep_why")])
        return out

    need_t, need_v = keys_missing(st)
    spend = Spend(task, st)
    for p in parts:
        if need_t:
            p.update(said=[], rooms=[], findings=[], heard_s=0.0)
        else:
            transcribe_part(p, st, spend, out["problems"])
    if need_t:
        out["problems"].append(need_t)
    frames_tmp = root / ("frames-%d" % int(task.get("id") or 0))
    try:
        plan = plan_frames(parts, st, rooms_known=True)
        pulled = pull_frames(parts, plan, frames_tmp, out["problems"])
        if need_v:
            out["problems"].append(need_v)
        else:
            read_frames(parts, pulled, st, spend, out["problems"])
        walk, used, spoken = build_walk(parts, pulled, many)
        out["frames_saved"] = _frames_to_workspace(task, parts, pulled, read=not need_v, many=many)
        if used:
            zname = stem + "_Walkthrough_Frames.zip"
            with zipfile.ZipFile(folder / zname, "w", zipfile.ZIP_DEFLATED) as z:
                for f in used:
                    z.write(f["path"], f["name"])
            out["files"].append(zname)
    finally:
        shutil.rmtree(frames_tmp, ignore_errors=True)
    walk_problem = ""
    if subj:
        walk, walk_problem = plan_walk(walk, str(inputs.get("plan") or ""), subj)
    if walk_problem:
        out["problems"].append(walk_problem)
    walk_name = stem + "_video_walk.json"
    (folder / walk_name).write_text(json.dumps(walk, indent=1, default=str), encoding="utf-8")
    keep_originals(parts, folder, stem, st)
    rows = index_entries(parts, used, spoken)
    out["files"] = write_index(folder, stem, out["street"], parts, rows, many, task) + out["files"]
    if any(p.get("heard_s") for p in parts):
        out["files"].append(write_transcript(folder, stem, out["street"], parts, many))
    out["files"] += [p["kept"] for p in parts if p.get("kept")] + [walk_name]
    rooms = []
    for p in parts:
        for _t, r in sorted(p.get("rooms") or []):
            if r and _room_key(r) not in {_room_key(x) for x in rooms}:
                rooms.append(r)
    if not rooms:
        for f in used:
            if f["room"] and _room_key(f["room"]) not in {_room_key(x) for x in rooms}:
                rooms.append(f["room"])
    conditions = Counter((f.get("seen") or {}).get("condition") for f in used)
    out.update(ok=True, mode="read", parts=[_public(p) for p in parts], rooms=rooms,
               conditions={k: v for k, v in conditions.items() if k and k != "unclear"},
               frames_read=sum(1 for f in pulled if f.get("read")), frames_used=len(used),
               heard_s=sum(p.get("heard_s") or 0 for p in parts),
               findings_said=len(spoken), said_money=[{"at": _at(by_n(parts, s["part"]), s["at"], many), "said": s["said"]}
                                                      for s in spoken if s["money"]][:5],
               money_at=money_moments(parts, many)[:6], walk_file=walk_name, index_rows=len(rows),
               kept_why=[p["keep_why"] for p in parts if p.get("keep_why")], skipped=list(spend.skipped),
               video_usd=round(spend.spent, 4))
    if subj and walk["photo_notes"]:
        ur = _underwrite(task, inputs, details, walk, folder)
        out["underwrite"] = {k: v for k, v in ur.items() if k != "run_dir"}
    elif subj:
        out["no_number"] = "nothing usable was read from the video, so there is no repair number from it yet"
    else:
        out["no_number"] = "no house was named, so there is no repair number: give the street line and ask again"
    out["spent_usd"] = round(store.spent(task_id=int(task.get("id") or 0)), 4)
    return out


def by_n(parts: list[dict], n: int) -> dict:
    return next(p for p in parts if p["n"] == n)


def _public(p: dict) -> dict:
    """One video as the task result keeps it: no work-folder path."""
    return {"n": p["n"], "name": p["name"], "source": p["source"], "seconds": round(p["seconds"], 1),
            "read_s": round(p.get("read_s") or 0, 1), "heard_s": round(p.get("heard_s") or 0, 1),
            "link": p.get("link") or "", "kept": p.get("kept") or "", "audio": bool(p.get("audio"))}


def _frames_to_workspace(task: dict, parts: list[dict], frames: list[dict], read: bool, many: bool) -> int:
    """The frames into the employee's own workspace for this thread, so a later turn can open them like
    photos (on a dry box that is the only way the video is looked at)."""
    if not frames or not (task.get("slack_channel") and task.get("slack_thread")):
        return 0
    by = {p["n"]: p for p in parts}
    rows = []
    for f in frames:
        seen = f.get("seen") or {}
        if read and not (seen and seen.get("usable")):
            continue
        label = _at(by[f["part"]], f["at"], True)
        room = seen.get("room") or f.get("hint") or ""
        if room:
            label += ", " + room.lower()
        if read and seen.get("condition") and seen["condition"] != "unclear":
            label += ", " + seen["condition"]
        rows.append({"src": f["path"], "name": f["name"], "label": label})
    got = attachments.add_frames(task["slack_channel"], task["slack_thread"], qs.workspace_of(task), rows, read=read,
                                 limit=max(1, settings()["max_frames"]))
    return len(got)


# ------------------------------------------------------------------------------- what is posted

def _files_lines(task: dict, r: dict) -> list[str]:
    """The links worth a click: the buyer index, the frames, the team's transcript and the kept video.
    The sheet copy of the index and the video's own walk stay in the folder (underwrite links the walk
    it used, and "task N" finds it)."""
    labels = (("_Walkthrough_Index.md", "Room index for buyers"), ("_Walkthrough_Frames.zip", "Frames from the video"),
              ("_Walkthrough_Transcript.md", "Narration (team only)"))
    out = []
    for f in r.get("files") or []:
        if Path(f).suffix.lower().lstrip(".") in attachments.VIDEO_EXT:
            out.append(qs.file_line(task, "Video kept for buyers", f))
            continue
        label = next((v for k, v in labels if f.endswith(k)), "")
        if label:
            out.append(qs.file_line(task, label, f))
    return out


def render(task: dict, r: dict) -> str:
    head = ":movie_camera: *Walkthrough video: %s*" % (r.get("street") or "this house")
    parts = r.get("parts") or []
    many = len(parts) > 1
    what = ("%d videos" % len(parts)) if many else "1 video"
    sources = ", ".join(dict.fromkeys(p.get("source") or "Slack" for p in parts))     # in the order posted
    lines = []
    if not r.get("ok"):
        lines = [head, "Not read: %s" % (r.get("why") or "no reason given")]
        lines += [":warning: " + qs.cap1(p) for p in (r.get("problems") or [])[:3]]
        return qs.safe_text("\n".join(lines))
    est = r.get("estimate") or {}
    mode = r.get("mode")
    if mode == "asked":
        lines = [head, "%s (%s), %s in all. Reading %s costs about $%.2f: %s of narration and up to %d frames." % (
            what, sources, minutes_text(est.get("seconds") or 0), "them" if many else "it", est.get("usd") or 0,
            minutes_text(est.get("read_s") or 0), est.get("frames") or 0)]
        if (est.get("seconds") or 0) > (est.get("read_s") or 0) + 1:
            lines.append("One ask reads the first %s (AGENTS_VIDEO_MAX_MIN); the rest would be left." % minutes_text(est["read_s"]))
        if r.get("run_cap") is not None and (est.get("usd") or 0) > float(r["run_cap"]):
            lines.append("That is more than one ask may spend ($%.2f, AGENTS_VIDEO_RUN_CAP_USD), so the reading would stop "
                         "at the cap." % float(r["run_cap"]))
        lines.append("Say go and I'll read %s." % ("them" if many else "it"))
        lines += [qs.cap1(n) for n in (r.get("notes") or [])[:2]]
        lines += [":warning: " + qs.cap1(p) for p in (r.get("problems") or [])[:2]]
        return qs.safe_text("\n".join(lines))
    if mode == "dry":
        lines = [head + "  (dry mode)",
                 "%s (%s), %s. Not read: this box is in dry mode, and reading costs about $%.2f "
                 "(AGENTS_VIDEO_DRY_PAID=1 lets a dry box read video)." % (
                     what, sources, minutes_text(est.get("seconds") or 0), est.get("usd") or 0)]
        if r.get("frames_saved"):
            lines.append("%d frames from the video are saved on this thread, so the rehab can be read from them like "
                         "photos: ask for it." % r["frames_saved"])
    else:
        lines = [head, "Read %s of narration from %s (%s) and %d frame%s; %d thing%s said about condition." % (
            minutes_text(r.get("heard_s") or 0) if r.get("heard_s") else "no", what.replace("1 video", "the video"), sources,
            r.get("frames_read") or 0, "" if (r.get("frames_read") or 0) == 1 else "s",
            r.get("findings_said") or 0, "" if (r.get("findings_said") or 0) == 1 else "s")]
        if r.get("rooms"):
            lines.append("Rooms: %s" % ", ".join(x.lower() for x in r["rooms"][:12]))
        if r.get("conditions"):
            lines.append("Frames by condition: %s" % ", ".join("%d %s" % (n, c) for c, n in
                                                                sorted(r["conditions"].items(), key=lambda kv: -kv[1])))
        ur = r.get("underwrite")
        if ur is not None:
            from . import underwrite as uw
            body = uw.render(task, ur).splitlines()[1:]
            lines += [ln for ln in body if not ln.startswith("Nothing written to DataSift")]
            if ur.get("ok") and ur.get("defaulted"):
                lines.append("Ask again with a plan (cosmetic, gut or gut t3) to price another of the engine's plans.")
        elif r.get("no_number"):
            lines.append(qs.cap1(r["no_number"]))
        for m in (r.get("said_money") or [])[:2]:
            lines.append(":warning: Said on the walk, not priced: %s (%s)" % (m["said"][:120], m["at"]))
        if r.get("money_at"):
            lines.append(":warning: Check the video before a buyer sees it: a dollar figure is said (%s)" %
                         ", ".join(r["money_at"][:4]))
    for n in (r.get("notes") or [])[:2]:
        lines.append(qs.cap1(n))
    for p in (list(r.get("problems") or []) + list(r.get("skipped") or []) + list(r.get("kept_why") or []))[:3]:
        lines.append(":warning: " + qs.cap1(p))
    lines += _files_lines(task, r)
    for p in parts:
        if p.get("link"):
            lines.append("Shared link kept for buyers: %s" % p["link"])
    spent = float(r.get("spent_usd") or 0)
    lines.append("Nothing written to DataSift%s." % (", spent $%.2f" % spent if spent else ", nothing spent"))
    return qs.safe_text("\n".join(lines))
