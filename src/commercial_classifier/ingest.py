"""Phase 1: any file -> canonical records in the work directory.

Nothing from the input is kept except the canonical fields and the polygon,
which keeps a statewide file manageable. The export re-reads the original file
by row number to put every input column back next to the results, after
checking the file has not changed since ingest.
"""
from __future__ import annotations

import collections
import hashlib
import json
import os
import re
import statistics

from . import io_tables, profiles, schema, store

RECORD_COLS = ["rid", "src_row", "county_key"] + schema.FIELDS


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_aliases(path):
    """`<file>.fields.json` written by fetch-parcels: {"fields": {name: alias}}."""
    side = path + ".fields.json"
    if not os.path.exists(side):
        return {}
    with open(side, encoding="utf-8") as f:
        data = json.load(f)
    return {alias: name for name, alias in (data.get("fields") or {}).items() if alias and alias != name}


def plan(path, profile=None, overrides=None, sheet=None):
    """Detect the profile and mapping without writing anything."""
    headers, rows = io_tables.read_table(path, sheet)
    aliases = load_aliases(path)
    name, score, scores = profiles.detect(headers)
    if profile:
        if profile not in profiles.PROFILES:
            raise profiles.ProfileError("unknown profile %r (choose: %s)" % (profile, ", ".join(profiles.PROFILES)))
        name = profile
    if name == "generic" and any(profiles.norm(h) == "objectid" for h in headers):
        name = "arcgis"
    mapping = profiles.build_mapping(name, headers, overrides, aliases)
    return headers, rows, name, score, scores, mapping


def _lot_is_sqft(mapping, sample_rows):
    spec = mapping.get("lot_acres")
    if not isinstance(spec, str) or profiles.norm(spec) not in profiles.AMBIGUOUS_LOT:
        return False
    vals = [schema.num(r.get(spec)) for r in sample_rows]
    vals = [v for v in vals if v]
    return bool(vals) and statistics.median(vals) > 200


def _state(v):
    s = (schema.text(v) or "").upper().strip()
    return s if re.match(r"^[A-Z]{2}$", s) else None


def _zip(v):
    s = re.sub(r"[^0-9]", "", str(schema.text(v) or ""))
    return s[:5].zfill(5) if len(s) >= 3 else None


def build_record(row, mapping, prof, lot_sqft):
    rec = {}
    for f in schema.FIELDS + ["lot_sqft", "situs_house", "situs_street"]:
        spec = mapping.get(f)
        if spec is None:
            rec[f] = None
            continue
        raw = profiles.read_value(row, spec)
        if f in prof.transforms:
            rec[f] = prof.transforms[f](raw)
        elif f in schema.NUMERIC:
            rec[f] = schema.num(raw)
        elif f in schema.DATES:
            rec[f] = schema.parse_date(raw)
        else:
            rec[f] = schema.text(raw)
    if lot_sqft and rec.get("lot_acres"):
        rec["lot_acres"] = rec["lot_acres"] / 43560.0
    if rec.get("lot_acres") is None and rec.get("lot_sqft"):
        rec["lot_acres"] = rec["lot_sqft"] / 43560.0
    if not rec.get("situs_address") and rec.get("situs_street"):
        rec["situs_address"] = " ".join(x for x in (rec.get("situs_house"), rec.get("situs_street")) if x)
    if rec.get("year_built") is not None and not (1600 <= rec["year_built"] <= 2100):
        rec["year_built"] = None
    lat, lon = rec.get("lat"), rec.get("lon")
    if lat is not None and lon is not None and (abs(lat) > 90 or abs(lon) > 180 or (lat == 0 and lon == 0)):
        rec["lat"] = rec["lon"] = None
    rec["situs_state"] = _state(rec.get("situs_state"))
    rec["mail_state"] = _state(rec.get("mail_state"))
    rec["situs_zip"] = _zip(rec.get("situs_zip"))
    rec["mail_zip"] = _zip(rec.get("mail_zip"))
    for f in ("lot_sqft", "situs_house", "situs_street"):
        rec.pop(f, None)
    return rec


def run(path, work, profile=None, overrides=None, sheet=None, state=None, county_key_override=None):
    headers, rows, name, score, scores, mapping = plan(path, profile, overrides, sheet)
    profiles.check_mapping(mapping, headers)
    prof = profiles.PROFILES[name]

    buffered = []
    for r in rows:
        buffered.append(r)
        if len(buffered) >= 2000:
            break
    lot_sqft = _lot_is_sqft(mapping, buffered)

    def all_rows():
        for r in buffered:
            yield r
        for r in rows:
            yield r

    con = store.connect(work)
    store.create_table(con, "records", [(c, "REAL" if c in schema.NUMERIC else
                                         "INTEGER" if c == "src_row" else "TEXT") for c in RECORD_COLS])
    store.create_table(con, "geom", [("rid", "TEXT"), ("geometry", "TEXT")])
    store.create_table(con, "dups", [("src_row", "INTEGER"), ("rid", "TEXT")], pk="src_row")
    geo_spec = mapping.get("geometry")
    seen, dups = {}, []
    stats = collections.Counter()
    state_counts = collections.Counter()
    batch, gbatch = [], []
    cols_sql = "INSERT INTO records (%s) VALUES (%s)" % (", ".join(RECORD_COLS), ", ".join("?" * len(RECORD_COLS)))
    for i, row in enumerate(all_rows()):
        rec = build_record(row, mapping, prof, lot_sqft)
        if not rec.get("situs_state") and state:
            rec["situs_state"] = state.upper()
        ck = schema.county_key(rec.get("fips"), rec.get("county"), rec.get("situs_state"), county_key_override)
        raw_items = sorted((k, str(v)) for k, v in row.items() if k != geo_spec)
        rid = schema.make_rid(ck, rec.get("apn"), rec.get("apn_seq"), rec.get("situs_address"),
                              rec.get("situs_zip"), raw_items)
        ch = schema.content_hash(raw_items)
        if rid in seen:
            if ch in seen[rid]:
                stats["exact_duplicates_dropped"] += 1
                dups.append((i, seen[rid][ch]))
                continue
            seen[rid][ch] = "%s-%s" % (rid, ch)
            rid = seen[rid][ch]
            stats["same_parcel_different_rows"] += 1
        else:
            seen[rid] = {ch: rid}
        if rec.get("situs_state"):
            state_counts[rec["situs_state"]] += 1
        batch.append([rid, i, ck] + [rec.get(f) for f in schema.FIELDS])
        if geo_spec:
            g = schema.text(profiles.read_value(row, geo_spec))
            if g:
                gbatch.append((rid, g))
        stats["rows"] += 1
        if len(batch) >= 5000:
            con.executemany(cols_sql, batch)
            batch = []
        if len(gbatch) >= 2000:
            con.executemany("INSERT INTO geom (rid, geometry) VALUES (?, ?)", gbatch)
            gbatch = []
    if batch:
        con.executemany(cols_sql, batch)
    if gbatch:
        con.executemany("INSERT INTO geom (rid, geometry) VALUES (?, ?)", gbatch)
    if dups:
        con.executemany("INSERT INTO dups (src_row, rid) VALUES (?, ?)", dups)
    con.commit()
    if not stats["rows"]:
        raise io_tables.InputError("no data rows in %s" % path)

    # a file with no situs state at all: fall back to --state, then the most
    # common mailing state, and say which one was used
    inferred = None
    if not state_counts:
        if state:
            inferred = state.upper()
        else:
            ms = con.execute("SELECT mail_state, COUNT(*) n FROM records WHERE mail_state IS NOT NULL "
                             "GROUP BY mail_state ORDER BY n DESC LIMIT 1").fetchone()
            inferred = ms[0] if ms else None
        if inferred:
            con.execute("UPDATE records SET situs_state = ? WHERE situs_state IS NULL", (inferred,))
            con.commit()
    mapped = {f: profiles.header_of(s) for f, s in mapping.items()}
    unmapped_headers = [h for h in headers if h not in {s for s in mapping.values() if isinstance(s, str)}]
    store.set_meta(con, "input", {"path": os.path.abspath(path), "sha256": file_sha256(path), "sheet": sheet,
                                  "headers": headers, "geometry_header": geo_spec if isinstance(geo_spec, str) else None})
    store.set_meta(con, "profile", {"name": name, "label": prof.label, "score": round(score, 2),
                                    "scores": {k: round(v, 2) for k, v in scores.items()},
                                    "mapping": mapped, "lot_sqft_converted": lot_sqft,
                                    "unmapped_headers": unmapped_headers[:200],
                                    "situs_state_inferred": inferred,
                                    "state_option": state, "county_key_override": county_key_override})
    out = dict(stats)
    out.update({"profile": name, "geometry_rows": con.execute("SELECT COUNT(*) FROM geom").fetchone()[0],
                "counties": con.execute("SELECT COUNT(DISTINCT county_key) FROM records").fetchone()[0]})
    store.mark_done(con, "ingest", out)
    con.close()
    return out
