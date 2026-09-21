#!/usr/bin/env python3
"""SiftStack readiness check, the plugin edition of `install.py --doctor`.

    python doctor.py                 # three buckets, fallback on the same line
    python doctor.py --env-dir DIR   # look for .env in DIR instead of the cwd
    python doctor.py --json          # same result, machine readable

Reads ../data/requires.json, which tools/build_skills.py --marketplace writes
INTO this plugin. It cannot read skills/manifest.json: an installed plugin is
copied to a cache and nothing outside its own directory comes with it.

No network, no spend, and it never holds or prints a credential's value. It
records only which NAMES are set.

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data" / "requires.json"
DOCS = "https://github.com/{repo}/blob/main/docs/setup"


def _names_set(env_dir: Path) -> set[str]:
    found = set()
    path = env_dir / ".env"
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if v.strip().strip("'\""):
                found.add(k.strip())
    return found


def check(env_dir: Path) -> dict:
    doc = json.loads(DATA.read_text(encoding="utf-8"))
    dotenv = _names_set(env_dir)
    have = lambda k: bool(os.environ.get(k)) or k in dotenv  # noqa: E731

    ready, login, blocked = [], [], []
    for p in doc["packages"]:
        req = p["requires"]
        missing = [k for k in req.get("env", []) if not have(k)]
        row = {"name": p["name"], "category": p["category"], "missing": missing,
               "accounts": req.get("accounts", []), "cost": req.get("cost", "Free"),
               "fallback": req.get("fallback")}
        if missing:
            blocked.append(row)
        elif row["accounts"]:
            # Keys are satisfied but the skill still drives a service you must
            # be signed in to. Calling that ready is a lie the person only
            # discovers when the browser lands on a login page.
            login.append(row)
        else:
            ready.append(row)
    return {"repo": doc["repo"], "total": len(doc["packages"]), "dotenv_names": len(dotenv),
            "env_dir": str(env_dir), "ready": ready, "login": login, "blocked": blocked}


def render(r: dict) -> str:
    docs = DOCS.format(repo=r["repo"])
    out = [f"SiftStack readiness check: {r['total']} packages. "
           "Nothing here sends a request or spends money.", ""]
    if r["dotenv_names"]:
        out += [f"Read {r['env_dir']}/.env with {r['dotenv_names']} value(s) set. "
                "Values are never displayed.", ""]
    else:
        out += [f"No .env found in {r['env_dir']}. Checked environment variables only.", ""]

    out.append(f"Works right now, nothing to configure ({len(r['ready'])} of {r['total']})")
    out += [f"  + {x['name']}" for x in r["ready"]]

    if r["login"]:
        out += ["", f"Keys are set, but you need to be signed in ({len(r['login'])})"]
        out += [f"  + {x['name']:<28} sign in to {', '.join(x['accounts'])}" for x in r["login"]]

    if r["blocked"]:
        usable = {x["name"] for x in r["ready"] + r["login"]}
        out += ["", f"Needs a credential ({len(r['blocked'])})"]
        for x in r["blocked"]:
            out.append(f"  - {x['name']:<28} set {', '.join(x['missing'])}")
            if x["cost"] and x["cost"] != "Free":
                out.append(f"      cost: {x['cost']}")
            fb = x["fallback"]
            if not fb:
                out.append("      without it: no substitute, this one needs the key")
            elif "#" in fb:
                page, anchor = fb.split("#", 1)
                out.append(f"      without it: {docs}/{page}.md#{anchor}")
            else:
                state = "ready now" if fb in usable else "also blocked"
                out.append(f"      without it: use the {fb} skill ({state})")

    accounts: dict[str, list[str]] = {}
    for x in r["ready"] + r["login"] + r["blocked"]:
        for a in x["accounts"]:
            accounts.setdefault(a, []).append(x["name"])
    if accounts:
        out += ["", "Logins these assume you already have (cannot be checked from here)"]
        for a in sorted(accounts):
            names = accounts[a]
            shown = ", ".join(names[:3]) + (f" +{len(names) - 3} more" if len(names) > 3 else "")
            out.append(f"  {a:<26} {shown}")

    out += ["", "Next",
            "  Ask for the siftstack setup skill to add keys, or fill in a .env by hand.",
            "  Every skill degrades rather than failing: a missing key skips that step.",
            f"  Setup guide:   {docs}/GETTING-STARTED.md",
            f"  No-API routes: {docs}/no-api-playbook.md", ""]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env-dir", default=".", help="folder holding your .env (default: cwd)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):  # Windows cp1252 consoles
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    result = check(Path(args.env_dir).expanduser().resolve())
    print(json.dumps(result, indent=2) if args.json else render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
