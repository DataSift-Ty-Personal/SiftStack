"""True owner: resolve recorded owner strings into entities, roll every parcel
up to its owner, then group owners that share a mailing address.

Linking (union-find over parcels):
  * same canonical name (generic institutions such as "FIRST BAPTIST CHURCH"
    also need the same mailing address)
  * same mailing address AND agreeing name tokens, star topology against the
    address's dominant name, so one ambiguous name cannot chain two
    governments together
  * name variants across addresses only with a co-owner as the second signal
An address behind many unrelated names (a bank, a servicer, a manager) is an
aggregator: it never links names that do not already agree.

Match tiers follow the "two signals" rule, and fix the original's 100% green:
  single   one parcel. Nothing was matched, so nothing is scored.
  green    two signals agree across 2+ parcels: one name at one mailing
           address (95), or name variants merged by a shared address or a
           co-owner (90)
  grey     one name with scattered mailing addresses (80 for a business, 65 for
           a person, since two people can share a name), or no mailing data

Out of state compares each parcel's mailing state to that parcel's own situs
state; an owner is Y, N or mixed.
"""
from __future__ import annotations

import collections
import hashlib
import os
import re

from . import schema, store

ENTITY_SUFFIX = (r"(LLC|L L C|INC|INCORPORATED|CORP|CORPORATION|LP|LLP|LLLP|LTD|CO|COMPANY|PLLC|PC|PA|TRUST|TR|"
                 r"TRUSTEE|TRUSTEES|ESTATE|PARTNERSHIP|PARTNERS|ASSOCIATES|ASSOC|ENTERPRISES|HOLDINGS|PROPERTIES|"
                 r"PROPERTY|GROUP|INVESTMENTS|VENTURES|REALTY|DEVELOPMENT|MANAGEMENT|FAMILY|REVOCABLE|LIVING|"
                 r"IRREVOCABLE|JT|JTRS)")
NOISE = re.compile(r"\b(ET AL|ETAL|ET UX|ETUX|C/O|THE|AND|&)\b")
PUNCT = re.compile(r"[^A-Z0-9 ]")
SUFFIX_RX = re.compile(r"\b" + ENTITY_SUFFIX + r"\b")
PERSON_SUFFIX = re.compile(r"\b(JR|SR|II|III|IV|MD|DDS|ESQ)\b")
JUNK = {"NOT AVAILABLE FROM THE DATA SOURCE", "NOT AVAILABLE", "CEMETERY", "UNKNOWN", "UNKNOWN OWNER",
        "OWNER UNKNOWN", "CURRENT OWNER", "TAXPAYER", "OWNER", "NONE", "NO NAME", "N A", "NA", "SEE NOTES",
        "VACANT", "ESTATE", "TRUST", "LLC", "PROPERTY OWNER", "CONFIDENTIAL", "WITHHELD"}
JUNK_FRAG = ("NOT AVAILABLE", "DATA SOURCE", "UNKNOWN", "TAX SALE", "CONFIDENTIAL", "WITHHELD", "CURRENT OWNER",
             "TAXPAYER", "NO NAME", "SEE NOTES", "OWNER OF RECORD")
GENERIC_INST = {"CHURCH", "BAPTIST", "METHODIST", "PRESBYTERIAN", "CATHOLIC", "LUTHERAN", "CEMETERY", "CHAPEL",
                "MINISTRIES", "TABERNACLE", "FELLOWSHIP", "CONGREGATION", "ASSEMBLY", "PARISH", "TEMPLE",
                "MOSQUE", "SYNAGOGUE"}
AGG_CUTOFF = 4
OWN_COLS = ["rid", "entity_id", "owner_name", "owner_type", "tier", "match_score", "portfolio_parcels",
            "portfolio_value", "out_of_state", "top_class", "group_id", "group_parcels", "group_type"]
ENT_COLS = ["entity_id", "display_name", "entity_type", "tier", "match_score", "name_variants",
            "name_variant_list", "mailing_address", "out_of_state", "parcel_count", "portfolio_value_est",
            "total_units", "asset_mix", "top_asset_class", "group_id", "sample_parcels"]
GRP_COLS = ["group_id", "lead_name", "member_entities", "member_names", "businesses", "people", "group_parcels",
            "group_value_est", "group_units", "asset_mix", "group_type", "tier"]
REL_COLS = ["party_a", "party_b", "edge_type", "evidence"]
_DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def norm_name(s):
    s = (s or "").upper().strip()
    s = PUNCT.sub(" ", s)
    s = NOISE.sub(" ", s)
    s = PERSON_SUFFIX.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def name_tokens(s):
    s = SUFFIX_RX.sub(" ", norm_name(s))
    return frozenset(p for p in s.split() if len(p) > 2)


def is_junk(canon):
    if not canon or len(canon) <= 2 or canon in JUNK:
        return True
    return any(fr in canon for fr in JUNK_FRAG)


def gov_patterns():
    out = []
    with open(os.path.join(_DATA, "gov_patterns.txt"), encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if line:
                out.append(re.compile(r"\b%s\b" % re.escape(line)))
    return out


class _UF:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        p = self.parent
        p.setdefault(x, x)
        while p[x] != x:
            p[x] = p[p[x]]
            x = p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _eid(canon, mail):
    return "E" + hashlib.sha1(("%s|%s" % (canon, mail)).encode("utf-8")).hexdigest()[:10].upper()


def resolve(parcels):
    """parcels: list of dicts with rid, owner, co (list), mail, mail_base,
    mail_state, situs_state, corp, value, units, asset_class.
    Returns (entities, parcel_entity, related, groups, group_of)."""
    n_names = len({norm_name(p["owner"]) for p in parcels})
    thr_one = max(25, int(0.0005 * n_names))
    thr_skip = max(60, int(0.0012 * n_names))
    df = collections.Counter()
    for p in parcels:
        p["canon"] = norm_name(p["owner"])
        p["tokens"] = name_tokens(p["owner"])
    seen_names = {}
    for p in parcels:
        seen_names.setdefault(p["canon"], p["tokens"])
    for toks in seen_names.values():
        for t in toks:
            df[t] += 1

    def agree(a, b):
        if not a or not b:
            return False
        shared = a & b
        if len(a) == 1 and len(b) == 1:
            tok = next(iter(shared), None)
            return bool(tok) and df[tok] <= thr_one
        return len(shared) >= 2

    gov = gov_patterns()

    def generic(p):
        """Same name is not enough for a church-type name, or for a two-word
        PERSON name made of common words (two different John Smiths). Those
        also need the same mailing address. A business with an entity suffix
        or a government body links on its name alone."""
        tokens = p["tokens"]
        if tokens & GENERIC_INST:
            return True
        if SUFFIX_RX.search(p["canon"]) or p["corp"] or any(rx.search(p["canon"]) for rx in gov):
            return False
        return len(tokens) <= 2 and all(df[t] >= thr_one for t in tokens)

    def name_key(p):
        # order-independent, so MONDAY ROBERT W and ROBERT W MONDAY are one name
        return ("T:" + " ".join(sorted(p["tokens"]))) if len(p["tokens"]) >= 2 else p["canon"]

    ok = [i for i, p in enumerate(parcels) if not is_junk(p["canon"])]
    uf = _UF()
    by_name = collections.defaultdict(list)
    by_mail = collections.defaultdict(set)
    for i in ok:
        uf.find(i)
        by_name[name_key(parcels[i])].append(i)
        if parcels[i]["mail_base"]:
            by_mail[parcels[i]["mail_base"]].add(parcels[i]["canon"])

    def unrelated(names):
        names = list(names)
        if len(names) > 40:
            return len(names)
        reps = []
        for nm in names:
            t = seen_names.get(nm, frozenset())
            if not any(agree(t, rt) for rt in reps):
                reps.append(t)
        return len(reps)

    aggregators = {m for m, names in by_mail.items() if len(names) >= AGG_CUTOFF and unrelated(names) >= AGG_CUTOFF}
    for name, idxs in by_name.items():
        if generic(parcels[idxs[0]]):
            sub = collections.defaultdict(list)
            for i in idxs:
                sub[parcels[i]["mail_base"]].append(i)
            for g in sub.values():
                for j in g[1:]:
                    uf.union(g[0], j)
        else:
            for j in idxs[1:]:
                uf.union(idxs[0], j)
    name_parcels = collections.Counter(parcels[i]["canon"] for i in ok)
    mail_groups = collections.defaultdict(dict)
    for i in ok:
        p = parcels[i]
        if p["mail_base"] and p["tokens"]:
            mail_groups[p["mail_base"]].setdefault(p["canon"], (i, p["tokens"]))
    addr_edges = []
    for m, byname in mail_groups.items():
        if len(byname) < 2:
            continue
        dom = max(byname, key=lambda nm: name_parcels[nm])
        di, dt = byname[dom]
        for nm, (i, t) in byname.items():
            if nm == dom:
                continue
            if agree(dt, t):
                uf.union(di, i)
            elif m not in aggregators:
                addr_edges.append((dom, nm, m))
    coedges = set()
    canon_idx = {}
    for i in ok:
        canon_idx.setdefault(parcels[i]["canon"], i)
    partners = collections.defaultdict(set)
    for i in ok:
        p = parcels[i]
        for c in p["co"]:
            cc = norm_name(c)
            if cc and cc != p["canon"] and not is_junk(cc):
                coedges.add((p["canon"], cc, p["rid"]))
                partners[p["canon"]].add(cc)
                partners[cc].add(p["canon"])
    by_tok = collections.defaultdict(list)
    for canon, i in canon_idx.items():
        t = parcels[i]["tokens"]
        if 1 <= len(t) <= 4:
            for tok in t:
                by_tok[tok].append((canon, i, t))
    seen_pairs = set()
    for tok, group in by_tok.items():
        if len(group) > thr_skip:
            continue
        for a in range(len(group)):
            for b in range(a + 1, len(group)):
                ca, ia, ta = group[a]
                cb, ib, tb = group[b]
                if (ca, cb) in seen_pairs:
                    continue
                seen_pairs.add((ca, cb))
                if uf.find(ia) == uf.find(ib) or not agree(ta, tb):
                    continue
                if partners[ca] & partners[cb] or cb in partners[ca]:
                    uf.union(ia, ib)

    clusters = collections.defaultdict(list)
    for i in ok:
        clusters[uf.find(i)].append(i)
    entities, parcel_entity, used_ids = [], {}, set()
    for root, idxs in clusters.items():
        ps = [parcels[i] for i in idxs]
        names = collections.Counter(p["canon"] for p in ps)
        mails = collections.Counter(p["mail_base"] for p in ps if p["mail_base"])
        disp = collections.Counter(p["owner"] for p in ps).most_common(1)[0][0]
        top_mail, top_n = mails.most_common(1)[0] if mails else ("", 0)
        is_entity = bool(SUFFIX_RX.search(norm_name(disp))) or any(p["corp"] for p in ps)
        n = len(idxs)
        if n == 1:
            tier, score = "single", None
        elif not mails:
            tier, score = "grey", 70 if is_entity else 60
        elif len(names) == 1 and top_n / float(n) >= 0.8 and top_n >= 2:
            tier, score = "green", 95
        elif len(names) > 1:
            tier, score = "green", 90
        else:
            tier, score = "grey", 80 if is_entity else 65
        oos = [p["mail_state"] != p["situs_state"] for p in ps if p["mail_state"] and p["situs_state"]]
        out_of_state = "" if not oos else "Y" if all(oos) else "N" if not any(oos) else "mixed"
        value = sum(p["value"] or 0 for p in ps)
        units = sum(p["units"] or 0 for p in ps)
        mix = collections.Counter(p["asset_class"] for p in ps)
        eid = _eid(names.most_common(1)[0][0], top_mail)
        while eid in used_ids:
            eid = _eid(eid, str(len(used_ids)))
        used_ids.add(eid)
        ent = {"entity_id": eid, "display_name": disp, "entity_type": "business" if is_entity else "person",
               "tier": tier, "match_score": score, "name_variants": len(names),
               "name_variant_list": " | ".join(list(names)[:4]), "mailing_address": top_mail,
               "out_of_state": out_of_state, "parcel_count": n, "portfolio_value_est": int(value),
               "total_units": int(units), "asset_mix": " | ".join("%s:%d" % kv for kv in mix.most_common(5)),
               "top_asset_class": mix.most_common(1)[0][0], "group_id": "",
               "sample_parcels": " ".join(p["apn_or_rid"] for p in ps[:25]), "_names": set(names)}
        entities.append(ent)
        for p in ps:
            parcel_entity[p["rid"]] = ent
    related = [(a, b, "co-owner", ev) for a, b, ev in sorted(coedges)]
    related += [(a, b, "shared-address", ev) for a, b, ev in addr_edges]

    # ownership groups: entities linked by shared-address edges
    by_canon = {}
    for e in entities:
        for nm in e["_names"]:
            by_canon.setdefault(nm, e["entity_id"])
    guf = _UF()
    for e in entities:
        guf.find(e["entity_id"])
    for a, b, kind, _ in related:
        if kind != "shared-address":
            continue
        ea, eb = by_canon.get(a), by_canon.get(b)
        if ea and eb and ea != eb:
            guf.union(ea, eb)
    emap = {e["entity_id"]: e for e in entities}
    members_of = collections.defaultdict(list)
    for e in entities:
        members_of[guf.find(e["entity_id"])].append(e["entity_id"])
    groups = []
    for _, members in sorted(members_of.items(), key=lambda kv: -sum(emap[m]["parcel_count"] for m in kv[1])):
        lead = max(members, key=lambda m: emap[m]["parcel_count"])
        names_up = " | ".join(emap[m]["display_name"] for m in members).upper()
        gtype = "government" if any(rx.search(names_up) for rx in gov) else "private"
        if len(members) > 25:
            gtype = "oversized-review"
        mix = collections.Counter()
        for m in members:
            for part in emap[m]["asset_mix"].split(" | "):
                if ":" in part:
                    k, v = part.rsplit(":", 1)
                    mix[k] += int(v)
        gid = "G" + hashlib.sha1("|".join(sorted(members)).encode("utf-8")).hexdigest()[:10].upper()
        tier = emap[members[0]]["tier"] if len(members) == 1 else "grey"
        groups.append({"group_id": gid, "lead_name": emap[lead]["display_name"], "member_entities": len(members),
                       "member_names": " | ".join(emap[m]["display_name"] for m in members[:8]),
                       "businesses": sum(1 for m in members if emap[m]["entity_type"] == "business"),
                       "people": sum(1 for m in members if emap[m]["entity_type"] == "person"),
                       "group_parcels": sum(emap[m]["parcel_count"] for m in members),
                       "group_value_est": sum(emap[m]["portfolio_value_est"] for m in members),
                       "group_units": sum(emap[m]["total_units"] for m in members),
                       "asset_mix": " | ".join("%s:%d" % kv for kv in mix.most_common(5)),
                       "group_type": gtype, "tier": tier})
        for m in members:
            emap[m]["group_id"] = gid
    return entities, parcel_entity, related, groups, {g["group_id"]: g for g in groups}


def portfolio_band(n):
    return "1" if n == 1 else "2-5" if n <= 5 else "6-20" if n <= 20 else "21-100" if n <= 100 else "100+"


def run(work):
    con = store.connect(work)
    store.require(con, "classify")
    has_val = store.done(con, "value")
    sql = ("SELECT r.rid, r.apn, r.owner1, r.owner2, r.owner3, r.owner4, r.owner_first, r.owner_last, "
           "r.owner_is_corp, r.mail_address, r.mail_city, r.mail_state, r.mail_zip, r.situs_state, "
           "c.asset_class %s FROM records r JOIN cls c USING (rid) %s" %
           ((", v.value_estimate, v.units_final", "LEFT JOIN val v USING (rid)") if has_val else ("", "")))
    parcels = []
    no_owner = 0
    for r in con.execute(sql):
        owner = r["owner1"] or " ".join(x for x in (r["owner_first"], r["owner_last"]) if x)
        if not owner:
            no_owner += 1
            continue
        street = schema.addr_base(r["mail_address"])
        where = (r["mail_zip"] or "") or schema.norm_addr(r["mail_city"])
        parcels.append({"rid": r["rid"], "apn_or_rid": r["apn"] or r["rid"], "owner": owner.strip(),
                        "co": [x for x in (r["owner2"], r["owner3"], r["owner4"]) if x],
                        "mail_base": ("%s|%s" % (street, where)) if street else "",
                        "mail_state": r["mail_state"], "situs_state": r["situs_state"],
                        "corp": bool(r["owner_is_corp"]), "asset_class": r["asset_class"],
                        "value": r["value_estimate"] if has_val else None,
                        "units": r["units_final"] if has_val else None})
    ints = {"portfolio_parcels", "group_parcels", "name_variants", "parcel_count", "total_units", "member_entities",
            "businesses", "people", "group_units"}
    reals = {"match_score", "portfolio_value", "portfolio_value_est", "group_value_est"}
    for t, cols in (("own", OWN_COLS), ("entities", ENT_COLS), ("groups", GRP_COLS)):
        store.create_table(con, t, [(c, "INTEGER" if c in ints else "REAL" if c in reals else "TEXT") for c in cols],
                           pk={"own": "rid", "entities": "entity_id", "groups": "group_id"}[t])
    store.create_table(con, "related", [(c, "TEXT") for c in REL_COLS], pk=None)
    if not parcels:
        stats = {"skipped": "no owner name column in this file"}
        store.mark_done(con, "owners", stats)
        con.close()
        return stats
    entities, pe, related, groups, gmap = resolve(parcels)
    store.insert_many(con, "entities", ENT_COLS, ([e[c] for c in ENT_COLS] for e in entities))
    store.insert_many(con, "groups", GRP_COLS, ([g[c] for c in GRP_COLS] for g in groups))
    store.insert_many(con, "related", REL_COLS, related)
    rows = []
    for rid, e in pe.items():
        g = gmap.get(e["group_id"]) or {}
        rows.append((rid, e["entity_id"], e["display_name"], e["entity_type"], e["tier"], e["match_score"],
                     e["parcel_count"], e["portfolio_value_est"], e["out_of_state"], e["top_asset_class"],
                     e["group_id"], g.get("group_parcels"), g.get("group_type")))
    store.insert_many(con, "own", OWN_COLS, rows)
    multi = [e for e in entities if e["parcel_count"] > 1]
    green = sum(1 for e in multi if e["tier"] == "green")
    stats = {"parcels_with_owner": len(parcels), "parcels_without_owner": no_owner, "entities": len(entities),
             "multi_parcel_entities": len(multi),
             "green_pct_of_multi_parcel": round(green * 100.0 / len(multi), 1) if multi else None,
             "groups_with_2plus_entities": sum(1 for g in groups if g["member_entities"] > 1),
             "related_party_edges": len(related),
             "out_of_state_entities": sum(1 for e in entities if e["out_of_state"] == "Y")}
    store.mark_done(con, "owners", stats)
    con.close()
    return stats
