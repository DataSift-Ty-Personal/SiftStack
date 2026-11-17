#!/usr/bin/env python
"""Scan a tree for personal data before it goes public.

    python tools/public_scan.py <dir>                       # the rules in tools/public_scan_rules.json
    python tools/public_scan.py <dir> --extra private/public_release.json   # plus the owner's private words
    python tools/public_scan.py <dir> --list-addresses      # every address-like string, to review a baseline

It flags, file by file and line by line:
  * an email address outside the example and service domains in the rules;
  * a North American phone number, unless its exchange is 555, 000 or 001, or the number is a public office
    listed in the rules;
  * a street address (house number, street, suffix; also the county roll's number-last form) whose street
    is not obviously invented ("Example", "Sample", "Test"...) and is not on the reviewed allow list;
  * a word the owner keeps out, stored in the rules as SHA-256 of the lowercase word so this public file
    does not spell it;
  * with --extra, the private plain-text patterns too (never used in CI).

It also reads the text inside .skill, .plugin and .zip archives, because those are what people download.
Exit 1 when anything is flagged. Standard library only, so it runs in CI.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "tools" / "public_scan_rules.json"

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,})")
PHONE = re.compile(r"(?<![\d.])\(?([2-9]\d{2})\)?[-. ]([2-9]\d{2})[-. ](\d{4})(?!\d)"
                   r"|\+1([2-9]\d{2})([2-9]\d{2})(\d{4})(?!\d)"
                   r"|[\"'](?:\+?1)?([2-9]\d{2})([2-9]\d{2})(\d{4})[\"']")
SUFFIX = (r"(?i:St|Street|Rd|Road|Dr|Drive|Ave|Avenue|Ln|Lane|Way|Ct|Court|Pike|Blvd|Boulevard|Cir|Circle|Pl|"
          r"Place|Trl|Trail|Hwy|Highway|Pkwy|Parkway|Ter|Terrace)")
ADDRESS = re.compile(r"\b\d{2,6}\s+(?:[NSEWnsew]\.?\s+)?(?:[A-Z][A-Za-z'.]+\s+){1,3}" + SUFFIX + r"\b")
ADDRESS_LAST = re.compile(r"\b(?:[A-Z][A-Z']+\s+){1,3}(?:DR|RD|ST|AVE|LN|WAY|CT|PIKE|BLVD|CIR|PL|TRL|HWY|PKWY)\s+\d{2,6}\b")
WORD = re.compile(r"[A-Za-z]+")
ESCAPE = re.compile(r"\\[ntr]")
ARCHIVES = (".skill", ".plugin", ".zip")


def _glob_rx(pattern: str) -> re.Pattern:
    """A glob with ** (any depth), * (one segment) and ? as a regex over posix paths."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile("^" + out + "$")


def matcher(patterns):
    rxs = [_glob_rx(p) for p in patterns or []]
    return lambda rel: any(r.match(rel) for r in rxs)


def load_rules(path: Path = RULES) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class Scanner:
    def __init__(self, rules: dict, extra: dict | None = None):
        self.rules = rules
        self.skip = matcher(rules.get("skip"))
        self.ok_domains = {d.lower() for d in rules.get("email_ok_domains", [])}
        self.ok_exchanges = set(rules.get("phone_ok_exchanges", []))
        self.ok_areas = set(rules.get("phone_ok_areas", []))
        self.ok_phones = {re.sub(r"\D", "", p) for p in rules.get("phone_allow", [])}
        self.ok_words = [w.lower() for w in rules.get("street_ok_words", [])]
        self.ok_addresses = {self._norm(a) for a in rules.get("address_allow", [])}
        self.hashed = set(rules.get("hashed_words", []))
        self.extra = [re.compile(p) for p in (extra or {}).get("scan_words", [])]
        if extra:
            self.ok_addresses |= {self._norm(a) for a in extra.get("public_addresses_ok", [])}
            self.ok_phones |= {re.sub(r"\D", "", p) for p in extra.get("public_phones_ok", [])}

    @staticmethod
    def _norm(s: str) -> str:
        return re.sub(r"\s+", " ", s.strip().lower())

    def _email_ok(self, domain: str) -> bool:
        d = domain.lower()
        return any(d == ok or d.endswith("." + ok) for ok in self.ok_domains)

    def _phone_ok(self, digits: str) -> bool:
        return digits[:3] in self.ok_areas or digits[3:6] in self.ok_exchanges or digits in self.ok_phones

    def _address_ok(self, s: str) -> bool:
        low = s.lower()
        return any(w in low for w in self.ok_words) or self._norm(s) in self.ok_addresses

    def addresses(self, text: str) -> list[str]:
        return [m.group(0) for rx in (ADDRESS, ADDRESS_LAST) for m in rx.finditer(text)]

    def scan_text(self, rel: str, text: str) -> list[tuple]:
        out = []
        for n, line in enumerate(text.splitlines(), 1):
            # A name inside a code string can sit right after an escape ("\nJane: ..."): read the escape as
            # a space, or the letter n glues onto the name and every word check misses it.
            line = ESCAPE.sub(" ", line)
            for m in EMAIL.finditer(line):
                if not self._email_ok(m.group(1)):
                    out.append((rel, n, "email", m.group(0)))
            for m in PHONE.finditer(line):
                digits = "".join(g for g in m.groups() if g)
                if not self._phone_ok(digits):
                    out.append((rel, n, "phone", m.group(0).strip("\"'")))
            for a in self.addresses(line):
                if not self._address_ok(a):
                    out.append((rel, n, "address", a))
            if self.hashed:
                for w in {w.lower() for w in WORD.findall(line)}:
                    if hashlib.sha256(w.encode()).hexdigest() in self.hashed:
                        out.append((rel, n, "word", "(a listed word)"))
            for rx in self.extra:
                for m in rx.finditer(line):
                    out.append((rel, n, "private", m.group(0)))
        return out

    def scan_tree(self, root: Path) -> list[tuple]:
        out = []
        for p in sorted(root.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(root).as_posix()
            if self.skip(rel):
                continue
            data = p.read_bytes()
            if p.suffix.lower() in ARCHIVES:
                try:
                    with zipfile.ZipFile(io.BytesIO(data)) as z:
                        for name in z.namelist():
                            inner = z.read(name)
                            if b"\0" in inner[:4096] or self.skip(name):
                                continue
                            out += self.scan_text("%s!%s" % (rel, name), inner.decode("utf-8", "replace"))
                except zipfile.BadZipFile:
                    out.append((rel, 0, "archive", "not a readable zip"))
                continue
            if b"\0" in data[:4096]:
                continue
            out += self.scan_text(rel, data.decode("utf-8", "replace"))
        return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("root", nargs="?", default=str(ROOT))
    ap.add_argument("--rules", default=str(RULES))
    ap.add_argument("--extra", help="a private JSON with scan_words and allow lists (local only, never in CI)")
    ap.add_argument("--list-addresses", action="store_true", help="print every address-like string, flagged or not")
    ap.add_argument("--quiet-words", action="store_true", help="never print the matched private text (CI logs are public)")
    a = ap.parse_args(argv)
    root = Path(a.root)
    extra = json.loads(Path(a.extra).read_text(encoding="utf-8")) if a.extra and Path(a.extra).exists() else None
    sc = Scanner(load_rules(Path(a.rules)), extra)
    if a.list_addresses:
        seen = {}
        for p in sorted(root.rglob("*")):
            if p.is_file() and not sc.skip(p.relative_to(root).as_posix()):
                b = p.read_bytes()
                if b"\0" not in b[:4096]:
                    for s in sc.addresses(b.decode("utf-8", "replace")):
                        seen.setdefault(s, p.relative_to(root).as_posix())
        for s, f in sorted(seen.items()):
            print("%s%-40s %s" % ("  " if sc._address_ok(s) else "! ", s, f))
        return 0
    found = sc.scan_tree(root)
    for rel, n, kind, s in found:
        shown = "(hidden)" if a.quiet_words and kind == "private" else s
        print("%s:%d  %-8s %s" % (rel, n, kind, shown))
    print("public scan: %d finding(s) in %s" % (len(found), root))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
