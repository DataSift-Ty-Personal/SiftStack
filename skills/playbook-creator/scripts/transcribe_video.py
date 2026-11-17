#!/usr/bin/env python3
"""Turn a narrated screen recording into SOP source material.

Extracts the audio from a video, transcribes it with an audio-capable model
through OpenRouter, and writes a markdown file with three sections:

    ## TRANSCRIPT   timestamped narration
    ## ACTIONS      every UI action, timestamped, with the exact label spoken
    ## JUDGMENT     every decision rule, warning, and reason-why stated

Optionally also extracts a video frame at each detected action timestamp,
which become the document's real screenshots.

Usage:
    python transcribe_video.py walkthrough.mp4
    python transcribe_video.py walkthrough.mp4 --frames-dir images/
    python transcribe_video.py walkthrough.mp4 --frames-dir images/ --crop 1920:910:0:125

Needs: ffmpeg + ffprobe on PATH (free), and OPENROUTER_API_KEY in the
environment or a .env file in the working directory. Cost is roughly
$0.002 per audio minute (an 8 minute video is about 2 cents). No key?
Skip this script and paste your recorder's own transcript (Loom, Zoom,
and Fireflies all auto-transcribe for free); you only lose the
frame-extraction timestamps.

Stdlib only, on purpose.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

DEFAULT_MODEL = "google/gemini-2.5-flash"

PROMPT = """This is the narration of a screen-recorded software walkthrough. \
Transcribe and analyze it. Output EXACTLY these three sections:

## TRANSCRIPT
The full narration with a [M:SS] timestamp at the start of each spoken segment (every 5-15 seconds).

## ACTIONS
A numbered list of every discrete UI action the narrator performs or describes, in order, each as:
[M:SS] ACTION | exact target named (button/menu/field label as spoken) | which screen/page
Only include real UI actions (clicks, typing, selections, navigation). Note where the narrator is clearly clicking even if not naming the button.

## JUDGMENT
Bullet list of every decision rule, warning, tip, or reason-why the narrator gives (the things not visible on screen)."""


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key and Path(".env").exists():
        for line in Path(".env").read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("OPENROUTER_API_KEY="):
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not key:
        die("OPENROUTER_API_KEY not found in environment or .env.\n"
            "No key? Paste your recorder's own transcript (Loom/Zoom/Fireflies) instead.")
    return key


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=True)
    except FileNotFoundError:
        die(f"{cmd[0]} not found on PATH. Install ffmpeg (free): https://ffmpeg.org")
    except subprocess.CalledProcessError as e:
        die(f"{cmd[0]} failed: {e.stderr.strip()[:300]}")


def video_duration(video: Path) -> float:
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", str(video)])
    return float(out.stdout.strip().splitlines()[0])


def transcribe(video: Path, model: str, key: str) -> str:
    with tempfile.TemporaryDirectory() as td:
        mp3 = Path(td) / "audio.mp3"
        run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-vn",
             "-acodec", "libmp3lame", "-b:a", "64k", str(mp3)])
        b64 = base64.b64encode(mp3.read_bytes()).decode()
    body = {"model": model, "messages": [{"role": "user", "content": [
        {"type": "text", "text": PROMPT},
        {"type": "input_audio", "input_audio": {"data": b64, "format": "mp3"}},
    ]}]}
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        resp = json.load(urllib.request.urlopen(req, timeout=600))
    except Exception as e:
        die(f"OpenRouter call failed: {e}")
    try:
        return resp["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        die(f"Unexpected OpenRouter response: {json.dumps(resp)[:300]}")


TS_RX = re.compile(r"^\[(\d+):(\d{1,2})\]\s*(.+)$")


def parse_actions(transcript_md: str) -> list[tuple[int, str]]:
    """Return (seconds, description) for each ACTIONS line."""
    actions, in_actions = [], False
    for line in transcript_md.splitlines():
        if line.startswith("## "):
            in_actions = line.strip() == "## ACTIONS"
            continue
        if in_actions:
            m = TS_RX.match(line.strip().lstrip("0123456789. "))
            if m:
                actions.append((int(m.group(1)) * 60 + int(m.group(2)), m.group(3)))
    return actions


def stated_end(transcript_md: str) -> int:
    """Last timestamp anywhere in the output, for clock-drift correction."""
    last = 0
    for m in re.finditer(r"\[(\d+):(\d{1,2})\]", transcript_md):
        last = max(last, int(m.group(1)) * 60 + int(m.group(2)))
    return last


def slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s[:n] or "action"


def extract_frames(video: Path, transcript_md: str, frames_dir: Path,
                   duration: float, crop: str | None) -> int:
    actions = parse_actions(transcript_md)
    if not actions:
        print("warn: no ACTIONS timestamps found; no frames extracted")
        return 0
    # Transcription clocks drift long. Scale stated time onto real time.
    stated = stated_end(transcript_md)
    scale = duration / stated if stated > duration else 1.0
    if scale != 1.0:
        print(f"clock drift detected: scaling timestamps by {scale:.3f}")
    frames_dir.mkdir(parents=True, exist_ok=True)
    vf = [f"crop={crop}"] if crop else []
    for i, (sec, desc) in enumerate(actions, 1):
        t = min(sec * scale, max(duration - 1, 0))
        out = frames_dir / f"f{i:02d}_{slug(desc)}.jpg"
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.1f}", "-i", str(video),
               "-frames:v", "1", "-q:v", "3"]
        if vf:
            cmd += ["-vf", vf[0]]
        run(cmd + [str(out)])
    print(f"extracted {len(actions)} frames to {frames_dir}")
    print("VERIFY EVERY FRAME against its step before embedding: drift correction")
    print("is approximate, and a frame can catch a page mid-load. Re-pull with a")
    print("nudged timestamp (ffmpeg -ss) where needed, and drop duplicates.")
    return len(actions)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("video", help="Path to the screen recording (mp4, mov, webm...)")
    ap.add_argument("--out", help="Output markdown path (default: <video>.transcript.md)")
    ap.add_argument("--frames-dir", help="Also extract a frame per detected action into this directory")
    ap.add_argument("--crop", help="ffmpeg crop w:h:x:y to remove browser chrome/taskbar "
                                   "(1080p Chrome full screen: 1920:910:0:125)")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"OpenRouter model (default {DEFAULT_MODEL})")
    args = ap.parse_args()

    video = Path(args.video)
    if not video.exists():
        die(f"video not found: {video}")
    key = api_key()
    duration = video_duration(video)
    print(f"video: {video.name} ({duration/60:.1f} min). Transcribing...")

    result = transcribe(video, args.model, key)
    out = Path(args.out) if args.out else video.with_suffix(".transcript.md")
    out.write_text(result, encoding="utf-8")
    n_actions = len(parse_actions(result))
    print(f"wrote {out} ({len(result)} chars, {n_actions} actions detected)")

    if args.frames_dir:
        extract_frames(video, result, Path(args.frames_dir), duration, args.crop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
