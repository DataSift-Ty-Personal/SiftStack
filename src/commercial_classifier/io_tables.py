"""Read any tabular property file as (headers, row iterator); write CSV.

CSV/TSV/TXT (delimiter sniffed, UTF-8 with a Latin-1 fallback), XLSX
(openpyxl), parquet (pyarrow), JSON/JSONL (a list of objects, or ArcGIS
features). Rows stream, so a statewide file never has to fit in memory.
"""
from __future__ import annotations

import csv
import importlib.util
import json
import os
import sys

csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))  # parcel polygons are long


class InputError(RuntimeError):
    pass


def has_module(name):
    return importlib.util.find_spec(name) is not None


def _encoding(path):
    with open(path, "rb") as f:
        head = f.read(1 << 20)
    try:
        head.decode("utf-8")
        return "utf-8-sig"
    except UnicodeDecodeError as e:
        # a multi-byte character cut at the 1MB boundary is still UTF-8
        if e.start >= len(head) - 3:
            return "utf-8-sig"
        return "cp1252"


def _sniff(path, enc):
    with open(path, encoding=enc, errors="replace", newline="") as f:
        sample = f.read(64 * 1024)
    first = sample.splitlines()[0] if sample else ""
    counts = {d: first.count(d) for d in (",", "\t", "|", ";")}
    best = max(counts, key=counts.get)
    return best if counts[best] else ","


def read_table(path, sheet=None):
    """Return (headers, iterator of dict rows). Header order is preserved."""
    if not os.path.exists(path):
        raise InputError("file not found: %s" % path)
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        return _read_xlsx(path, sheet)
    if ext == ".xls":
        raise InputError("old .xls workbooks are not supported: save it as .xlsx or .csv")
    if ext == ".parquet":
        return _read_parquet(path)
    if ext in (".json", ".geojson"):
        return _read_json(path)
    if ext == ".jsonl":
        return _read_jsonl(path)
    return _read_csv(path)


def _read_csv(path):
    enc = _encoding(path)
    delim = _sniff(path, enc)
    f = open(path, encoding=enc, errors="replace", newline="")
    rdr = csv.reader(f, delimiter=delim)
    try:
        headers = next(rdr)
    except StopIteration:
        f.close()
        raise InputError("empty file: %s" % path)
    headers = [h.strip() for h in headers]
    headers = _dedupe(headers)

    def rows():
        try:
            for rec in rdr:
                if not any(c.strip() for c in rec):
                    continue
                if len(rec) < len(headers):
                    rec = rec + [""] * (len(headers) - len(rec))
                yield dict(zip(headers, rec))
        finally:
            f.close()
    return headers, rows()


def _dedupe(headers):
    seen, out = {}, []
    for h in headers:
        h = h or "column"
        if h in seen:
            seen[h] += 1
            out.append("%s_%d" % (h, seen[h]))
        else:
            seen[h] = 1
            out.append(h)
    return out


def _read_xlsx(path, sheet):
    if not has_module("openpyxl"):
        raise InputError("reading .xlsx needs openpyxl (pip install openpyxl), or save the sheet as .csv")
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb[sheet] if sheet else wb.worksheets[0]
    it = ws.iter_rows(values_only=True)
    try:
        headers = [("" if h is None else str(h)).strip() for h in next(it)]
    except StopIteration:
        raise InputError("empty sheet in %s" % path)
    headers = _dedupe(headers)

    def rows():
        for rec in it:
            if rec is None or not any(c not in (None, "") for c in rec):
                continue
            yield {h: ("" if v is None else v) for h, v in zip(headers, rec)}
        wb.close()
    return headers, rows()


def _read_parquet(path):
    if not has_module("pyarrow"):
        raise InputError("reading .parquet needs pyarrow (pip install pyarrow), or export the file as .csv")
    import pyarrow.parquet as pq
    pf = pq.ParquetFile(path)
    headers = [h.strip().strip('"') for h in pf.schema_arrow.names]
    raw_names = pf.schema_arrow.names

    def rows():
        for batch in pf.iter_batches(batch_size=20000):
            for rec in batch.to_pylist():
                yield {h: ("" if rec.get(r) is None else rec.get(r)) for h, r in zip(headers, raw_names)}
    return headers, rows()


def _flatten(obj):
    """ArcGIS feature -> attributes (+ geometry kept as JSON); GeoJSON feature
    -> properties (+ geometry); plain object -> itself."""
    if isinstance(obj, dict) and "attributes" in obj:
        row = dict(obj.get("attributes") or {})
        if obj.get("geometry"):
            row["geometry"] = json.dumps(obj["geometry"])
        return row
    if isinstance(obj, dict) and obj.get("type") == "Feature":
        row = dict(obj.get("properties") or {})
        if obj.get("geometry"):
            row["geometry"] = json.dumps(obj["geometry"])
        return row
    return obj


def _rows_with_headers(objs):
    objs = [_flatten(o) for o in objs]
    headers = []
    seen = set()
    for o in objs[:1000]:
        for k in o:
            if k not in seen:
                seen.add(k)
                headers.append(k)
    return headers, iter(objs)


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict):
        data = data.get("features") or data.get("rows") or data.get("data") or []
    if not isinstance(data, list):
        raise InputError("%s: expected a list of records or a features array" % path)
    return _rows_with_headers(data)


def _read_jsonl(path):
    objs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                objs.append(json.loads(line))
    return _rows_with_headers(objs)


def write_csv(path, headers, rows):
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    n = 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for r in rows:
            w.writerow(["" if r.get(h) is None else r.get(h) for h in headers])
            n += 1
    return n


def read_csv_dicts(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))
