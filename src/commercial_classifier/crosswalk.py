"""Code systems and the resolver that turns a record's use signals into a label.

A record can carry several use signals (an FA code, a structure type, a vendor
property type, a county code, a property class, zoning). Each becomes a KEY:
(system, county_key, code, desc). A key resolves, in order, through:

  1. a built-in table (FA codes, the FA description index, LBCS)
  2. a learned crosswalk row (Claude or a human labelled that distinct value)
  3. the keyword classifier (capped at 0.6)

National systems (FA, structure type, vendor type, LBCS) use county_key '*';
county-native systems (a county's own use code, property class, zoning) are
keyed by county, so a code learned in one county is never applied in another.
"""
from __future__ import annotations

import csv
import glob
import hashlib
import os
import re
from collections import namedtuple

from . import keywords, taxonomy

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

Label = namedtuple("Label", "asset_class sub_type confidence provenance method reason source target_hint")
Key = namedtuple("Key", "system ck code desc")

CAPS = {"builtin": 1.0, "human": 0.9, "claude": 0.75, "keyword": 0.6, "derived": 0.6}
PROV = {"builtin": "sourced", "human": "learned-human", "claude": "learned-claude",
        "keyword": "learned-keyword", "derived": "derived"}
NATIONAL = {"fa", "structure_type", "vendor_type", "lbcs", "datasift_use", "datasift_structure"}
COARSE = {"property_class", "zoning", "datasift_use"}
COARSE_CAP = 0.5
XW_COLS = ["system", "county_key", "code", "desc", "asset_class", "sub_type", "confidence", "source", "notes"]

_LETTER_CLASS = {"I": ("Industrial", "", 0.35), "A": ("Land", "Agricultural", 0.35),
                 "E": ("Civic/Exempt", "", 0.4), "R": ("Residential", "", 0.4)}


def desc_key(s):
    return re.sub(r"[^A-Z0-9]+", " ", (s or "").upper()).strip()


def desc_hash(s):
    return hashlib.sha1(desc_key(s).encode("utf-8")).hexdigest()[:12]


def code_key(system, code):
    s = str(code or "").strip()
    if not s:
        return ""
    if re.match(r"^\d+\.0+$", s):
        s = s.split(".")[0]
    if system == "fa" and s.isdigit():
        return s.zfill(4)
    return desc_key(s) if re.search(r"[A-Za-z]{3}", s) else s.upper()


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


class Crosswalks:
    def __init__(self, work=None, extra=()):
        self.fa = {}
        with open(os.path.join(DATA, "xw_fa.csv"), encoding="utf-8") as f:
            for r in csv.DictReader(f):
                self.fa[r["code"]] = (r["asset_class"], r["sub_type"], _f(r["confidence"], 0.5), r["target_hint"])
        self.fa_desc = {}
        with open(os.path.join(DATA, "fa_desc_index.csv"), encoding="utf-8") as f:
            for r in csv.DictReader(f):
                self.fa_desc[r["desc_sha1_12"]] = r["code"]
        self.lbcs = {}
        with open(os.path.join(DATA, "lbcs.csv"), encoding="utf-8") as f:
            for r in csv.DictReader(f):
                self.lbcs[r["prefix"]] = (r["asset_class"], r["sub_type"], _f(r["confidence"], 0.35))
        self.learned = {}
        self.sources = []
        for p in sorted(glob.glob(os.path.join(DATA, "crosswalks", "*.csv"))):
            self.load(p)
        if work:
            lp = learned_path(work)
            if os.path.exists(lp):
                self.load(lp)
        for p in extra or ():
            self.load(p)

    def load(self, path):
        n = 0
        with open(path, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                if not (r.get("asset_class") or "").strip():
                    continue
                try:
                    ac, st = taxonomy.validate(r["asset_class"], r.get("sub_type", ""))
                except taxonomy.TaxonomyError:
                    continue
                src = (r.get("source") or "claude").strip().lower()
                if src not in CAPS:
                    src = "claude"
                conf = min(_f(r.get("confidence"), CAPS[src]) or CAPS[src], CAPS[src])
                system = (r.get("system") or "").strip()
                k = (system, (r.get("county_key") or "*").strip().upper() or "*",
                     code_key(system, r.get("code")), desc_key(r.get("desc")))
                self.learned[k] = (ac, st, conf, src, (r.get("notes") or "").strip())
                n += 1
        self.sources.append((path, n))
        return n

    # ---- per-key resolution ------------------------------------------------
    def _learned(self, key):
        for ck in (key.ck, "*"):
            for code, desc in ((key.code, key.desc), (key.code, ""), ("", key.desc)):
                if not code and not desc:
                    continue
                hit = self.learned.get((key.system, ck, code, desc))
                if hit:
                    return hit
        return None

    def _fa_label(self, code, method):
        ac, st, conf, hint = self.fa.get(code, ("Other", "", 0.3, ""))
        return Label(ac, st, conf, "sourced", method, "FA code %s" % code, "builtin", hint)

    def table_label(self, key):
        """Built-in or learned label for a key, or None. No keywords."""
        if key.system == "fa" and key.code:
            return self._fa_label(key.code, "tier0-xw:fa")
        hit = self._learned(key)
        if hit:
            ac, st, conf, src, notes = hit
            method = "tier0-xw:%s" % key.system if src == "builtin" else "tier0-learned:%s" % key.system
            return Label(ac, st, conf, PROV[src], method, notes or "%s crosswalk" % src, src, "")
        if key.desc:
            code = self.fa_desc.get(desc_hash(key.desc))
            if code:
                lab = self._fa_label(code, "tier0-xw:fa-desc")
                return lab._replace(reason="FA description match (code %s)" % code)
        if key.system == "lbcs" and key.code[:1] in self.lbcs:
            ac, st, conf = self.lbcs[key.code[:1]]
            return Label(ac, st, conf, "sourced", "tier0-xw:lbcs", "LBCS %s" % key.code, "builtin", "")
        return None

    def keyword_label(self, key):
        """Keyword label for a key: (Label or None, conflicting Hit or None)."""
        text = key.desc or (key.code if re.search(r"[A-Z]{3}", key.code) else "")
        if key.system == "zoning":
            hit = keywords.zoning(key.code, key.desc)
            if not hit:
                return None, None
            return Label(hit.asset_class, hit.sub_type, hit.confidence, "derived", "tier1-zoning",
                         hit.reason, "derived", ""), None
        if key.system == "property_class" and not text and key.code in _LETTER_CLASS:
            ac, st, conf = _LETTER_CLASS[key.code]
            return Label(ac, st, conf, "derived", "tier1-property-class", "property class '%s'" % key.code,
                         "derived", ""), None
        if not text:
            return None, None
        hit, conflict = keywords.use(text)
        if not hit:
            return None, None
        conf = min(hit.confidence, 0.5 if key.system == "property_class" else CAPS["keyword"])
        return Label(hit.asset_class, hit.sub_type, conf, "learned-keyword", "tier1-keyword:%s" % key.system,
                     hit.reason, "keyword", ""), conflict

    def label(self, key):
        lab = self.table_label(key)
        conflict = None
        if not lab:
            lab, conflict = self.keyword_label(key)
        # a coarse signal (a one-word property class, zoning, a vendor's
        # "Residential / Commercial" bucket) never outranks a specific use code,
        # however confident the label on the coarse value is
        if lab and key.system in COARSE and lab.confidence > COARSE_CAP:
            lab = lab._replace(confidence=COARSE_CAP)
        return lab, conflict


def use_keys(rec, prof, ck):
    """The record's use signals as keys, highest priority first."""
    keys = []

    def add(system, code, desc, national):
        c, d = code_key(system, code), desc_key(desc)
        if c or d:
            keys.append(Key(system, "*" if national else (ck or ""), c, d))

    get = rec.get
    if prof.name == "fa":
        add("fa", get("use_code"), None, True)
    else:
        add(prof.use_system, get("use_code"), get("use_desc"), prof.use_national)
    add("structure_type", None, get("structure_type"), True)
    vt = " ".join(x for x in (get("vendor_type"), get("vendor_subtype")) if x)
    add("vendor_type", None, vt, True)
    add("lbcs", get("lbcs_code"), get("lbcs_desc"), True)
    add("county_use", get("county_use_code"), get("county_use_desc"), False)
    if prof.name != "fa":
        add("property_class", get("property_class"), None, False)
    add("zoning", get("zoning"), get("zoning_desc"), False)
    return keys


def resolve(xw, rec, prof, ck):
    """Best label for a record across all its keys.

    Returns (label, key, all_labels, flags). Two keys from different systems
    that agree lift a non-sourced label by 0.1 (cap 0.9); two confident keys
    that disagree are flagged for review."""
    keys = use_keys(rec, prof, ck)
    found, flags = [], []
    for i, k in enumerate(keys):
        lab, conflict = xw.label(k)
        if lab:
            found.append((i, k, lab))
            if conflict and lab.source in ("keyword", "derived"):
                flags.append("mixed-use text: %s / %s" % (lab.asset_class, conflict.asset_class))
    if not found:
        return None, (keys[0] if keys else None), [], flags
    found.sort(key=lambda t: (-t[2].confidence, t[0]))
    _, best_key, best = found[0]
    # a county code refines a generic national code (the Knox vacant-land fix)
    if best.provenance == "sourced" and best.confidence <= 0.7:
        for _, k, lab in found[1:]:
            if k.system == "county_use" and lab.source in ("builtin", "claude", "human") \
                    and lab.confidence >= best.confidence:
                method = "tier1-vacant-county" if lab.asset_class == "Land" else "tier0-county"
                best = lab._replace(method=method, provenance="derived" if lab.source == "builtin" else lab.provenance,
                                    reason="county code %s refines FA %s" % (k.code, best.reason),
                                    target_hint=best.target_hint)
                best_key = k
                break
    others = [(k, lab) for _, k, lab in found if k is not best_key]
    agree = [lab for k, lab in others if lab.asset_class == best.asset_class and k.system != best_key.system]
    if agree and best.provenance != "sourced" and best.confidence < 0.9:
        best = best._replace(confidence=round(min(0.9, best.confidence + 0.1), 2),
                             reason=best.reason + "; agrees with " + agree[0].reason)
    for k, lab in others:
        if lab.asset_class != best.asset_class and lab.confidence >= 0.6 and best.confidence < 0.9:
            flags.append("signals disagree: %s says %s, %s says %s" % (
                best_key.system, best.asset_class, k.system, lab.asset_class))
            break
    return best, best_key, [lab for _, _, lab in found], flags


def learned_path(work):
    return os.path.join(work, "crosswalks", "learned.csv")
