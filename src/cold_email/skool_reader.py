"""Read a Skool classroom you are a member of into local files.

Opens a real browser window, waits for you to sign in once, saves the session
(gitignored), then walks every course and module in the group and writes each
lesson's text, video link and attachments to output/cold_email/course/.

Course content is paid material: it stays in output/ (gitignored) for our own
use and is never committed to this public repo.

    python src/cold_email/skool_reader.py                    # b2c-cold-email-lab
    python src/cold_email/skool_reader.py --group <slug> --wait-min 25
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "output" / "cold_email" / "course"
STATE = ROOT / "output" / "cold_email" / "skool_state.json"
BASE = "https://www.skool.com"
NEXT_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def next_data(page) -> dict:
    m = NEXT_RE.search(page.content())
    if not m:
        return {}
    try:
        return json.loads(m.group(1)).get("props", {}).get("pageProps", {}) or {}
    except ValueError:
        return {}


def logged_in(context) -> bool:
    return any(c.get("name") == "auth_token" and c.get("value") for c in context.cookies(BASE))


def nodes(obj, path=()):
    """Every dict in the tree that looks like a Skool course unit."""
    if isinstance(obj, dict):
        md = obj.get("metadata")
        if isinstance(md, dict) and md.get("title") and (obj.get("id") or obj.get("name")):
            yield path, obj
        for k, v in obj.items():
            yield from nodes(v, path + (k,))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from nodes(v, path + (i,))


def tree(course_props: dict) -> list[dict]:
    """Flatten the course payload into ordered units with their parent set."""
    root = course_props.get("course") or {}
    out: list[dict] = []

    def walk(node, depth, parent):
        c = node.get("course") if isinstance(node.get("course"), dict) else node
        md = (c or {}).get("metadata") or {}
        if c and md.get("title"):
            out.append({
                "id": c.get("id"), "name": c.get("name"), "depth": depth,
                "unit": c.get("unitType") or md.get("unitType"),
                "title": md.get("title"), "parent": parent,
            })
        for ch in node.get("children") or []:
            walk(ch, depth + 1, md.get("title") or parent)

    if root:
        walk(root, 0, None)
    if len(out) <= 1:  # unknown shape: fall back to every titled node
        seen = set()
        for _, n in nodes(course_props):
            if n.get("id") in seen:
                continue
            seen.add(n.get("id"))
            md = n["metadata"]
            out.append({"id": n.get("id"), "name": n.get("name"), "depth": None,
                        "unit": n.get("unitType"), "title": md.get("title"), "parent": None})
    return out


def find_unit(props: dict, unit_id: str) -> dict:
    for _, n in nodes(props):
        if n.get("id") == unit_id:
            return n
    return {}


def rich_text(desc) -> str:
    """Skool stores lesson text as markdown or as a '[v2]' JSON node list."""
    if not desc:
        return ""
    if isinstance(desc, str) and desc.startswith("[v2]"):
        try:
            desc = json.loads(desc[4:])
        except ValueError:
            return desc
    if isinstance(desc, str):
        return desc
    parts: list[str] = []

    def walk(n):
        if isinstance(n, list):
            for x in n:
                walk(x)
            return
        if not isinstance(n, dict):
            return
        if n.get("text"):
            parts.append(n["text"])
        for k in ("content", "children"):
            walk(n.get(k))
        if n.get("type") in ("paragraph", "heading", "listItem", "bulletList", "orderedList"):
            parts.append("\n")

    walk(desc)
    return re.sub(r"\n{3,}", "\n\n", "".join(parts)).strip()


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:60] or "untitled"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", default="b2c-cold-email-lab")
    ap.add_argument("--wait-min", type=float, default=25.0)
    ap.add_argument("--delay", type=float, default=1.5)
    a = ap.parse_args()

    from playwright.sync_api import sync_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(storage_state=str(STATE) if STATE.exists() else None,
                                      viewport={"width": 1280, "height": 900})
        page = context.new_page()
        page.goto(f"{BASE}/{a.group}/classroom", wait_until="domcontentloaded")
        if not logged_in(context):
            page.goto(f"{BASE}/login", wait_until="domcontentloaded")
            log("Sign in to Skool in the browser window. Waiting...")
            deadline = time.time() + a.wait_min * 60
            while not logged_in(context):
                if time.time() > deadline:
                    log("Gave up waiting for sign-in.")
                    return 2
                time.sleep(2)
            log("Signed in.")
        context.storage_state(path=str(STATE))

        page.goto(f"{BASE}/{a.group}/classroom", wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        cls = next_data(page)
        (OUT / "_classroom_props.json").write_text(json.dumps(cls, indent=1), encoding="utf-8")
        courses = []
        for _, n in nodes(cls):
            name = str(n.get("name") or "")
            if re.fullmatch(r"[0-9a-f]{6,12}|\d{6,10}", name) and n.get("id") not in {c["id"] for c in courses}:
                courses.append({"id": n.get("id"), "name": name, "title": n["metadata"]["title"]})
        log(f"Courses found: {len(courses)} " + "; ".join(c["title"] for c in courses))
        if not courses:
            log("No courses parsed; raw props saved to _classroom_props.json for a look.")
            return 3

        index = []
        for ci, c in enumerate(courses, 1):
            page.goto(f"{BASE}/{a.group}/classroom/{c['name']}", wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            props = next_data(page)
            (OUT / f"_course_{c['name']}_props.json").write_text(json.dumps(props, indent=1), encoding="utf-8")
            units = [u for u in tree(props) if u["id"] != c["id"]]
            log(f"Course {ci}: {c['title']} -> {len(units)} units")
            cdir = OUT / f"{ci:02d}-{slug(c['title'])}"
            cdir.mkdir(exist_ok=True)
            for ui, u in enumerate(units, 1):
                if u.get("unit") == "set":  # a section heading, no page of its own
                    index.append({**u, "course": c["title"], "file": None})
                    continue
                page.goto(f"{BASE}/{a.group}/classroom/{c['name']}?md={u['id']}", wait_until="domcontentloaded")
                page.wait_for_timeout(int(a.delay * 1000))
                mp = next_data(page)
                n = find_unit(mp, u["id"]) or find_unit(props, u["id"])
                md = n.get("metadata") or {}
                try:
                    shown = page.locator("main").inner_text(timeout=4000)
                except Exception:
                    shown = ""
                frames = [f.url for f in page.frames if f.url and f.url != page.url]
                rec = {
                    "course": c["title"], "section": u.get("parent"), "title": u["title"], "id": u["id"],
                    "video": md.get("videoLink") or md.get("videoUrl"),
                    "video_len_ms": md.get("videoLenMs"),
                    "resources": md.get("resources"),
                    "text": rich_text(md.get("desc")),
                    "page_text": shown,
                    "frames": frames,
                    "metadata_keys": sorted(md.keys()),
                }
                f = cdir / f"{ui:02d}-{slug(u['title'])}.json"
                f.write_text(json.dumps(rec, indent=1, ensure_ascii=False), encoding="utf-8")
                index.append({**u, "course": c["title"], "file": str(f.relative_to(OUT)),
                              "video": rec["video"], "chars": len(rec["text"] or rec["page_text"])})
                log(f"  {ui:02d} {u['title'][:70]} ({index[-1]['chars']} chars, video={'yes' if rec['video'] else 'no'})")
        (OUT / "index.json").write_text(json.dumps(index, indent=1, ensure_ascii=False), encoding="utf-8")
        context.storage_state(path=str(STATE))
        browser.close()
    log(f"Done: {sum(1 for i in index if i['file'])} lessons in {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
