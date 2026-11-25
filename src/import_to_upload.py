"""Turn an import CSV into the DataSift upload layout, and read one uploaded record back.

The courthouse photo import, the PDF import and a county CSV run through the enrichment write the
IMPORT layout (lowercase columns: address, owner_name, notice_type ...). The uploader that creates
records on the Open API key (`datasift_api_upload.py`) reads the UPLOAD layout (Property Street,
Owner First Name, Lists, Tags ...). Before this command the only bridge between the two was the
browser upload wizard, which needs a DataSift login. This is the bridge on the key.

    python src/import_to_upload.py convert output/photo_import_2026-11-24.csv
    python src/import_to_upload.py convert <import.csv> --out upload_probate_2026-11-24.csv --out-dir output/first-to-market-chain
    python src/datasift_api_upload.py --csv <upload.csv>                     # dry run
    python src/datasift_api_upload.py --csv <upload.csv> --limit 1 --commit  # one record
    python src/import_to_upload.py readback --street "123 Example St" --zip 37920

`convert` writes nothing to DataSift. `readback` only reads, on the Open API key in .env
(REISIFT_API_KEY): the owner, lists, tags and filled custom fields of the record at that street, so
the first uploaded record can be compared with the row that was sent before the rest go.
"""
from __future__ import annotations

import argparse
import os
import sys


def convert(src: str, out: str | None, out_dir: str | None) -> int:
    if out_dir:
        os.environ["SIFTSTACK_OUTPUT_DIR"] = out_dir
        os.makedirs(out_dir, exist_ok=True)
    # config reads SIFTSTACK_OUTPUT_DIR when it is first imported, so these come after the env.
    from data_formatter import read_csv
    from datasift_formatter import write_datasift_csv

    if not os.path.isfile(src):
        print(f"No such file: {src}")
        return 2
    notices = read_csv(src)
    if not notices:
        print(f"{src} has no rows in the import layout (address, owner_name, notice_type ...).")
        return 1
    path = write_datasift_csv(notices, filename=out)
    print(f"read {len(notices)} rows from {src}")
    print(f"wrote {path}")
    print("next: python src/datasift_api_upload.py --csv " + str(path) + "   (a dry run; nothing is written)")
    return 0


def readback(street: str, zip_code: str) -> int:
    import datasift_api_upload as up

    api = up.Api()
    uuid = up.find_property(api, {"street": street, "postal_code": zip_code})
    if not uuid:
        print(f"not found: {street} {zip_code}. Write the street the way DataSift stores it "
              "(Street as St, Road as Rd), and allow a minute for the search to catch up.")
        return 1
    rec = api.call(f"/api/internal/property/{uuid}/") or {}
    owner = rec.get("owner") or {}
    print("record:", uuid)
    print("owner:", " ".join(x for x in (owner.get("first_name"), owner.get("last_name")) if x)
          or owner.get("company") or "(none)")
    print("lists:", ", ".join(up._names(rec.get("lists"))) or "(none)")
    print("tags:", ", ".join(up._names(rec.get("tags"))) or "(none)")
    notes = up.record_notes(api, uuid, rec).strip()
    print("notes:", (notes[:300] + (" ..." if len(notes) > 300 else "")) if notes else "(none)")
    try:
        fields = api.call(f"/api/internal/property/{uuid}/custom-field/") or []
    except Exception as exc:  # a read of the fields failing must not hide the rest
        print("custom fields: could not read (" + exc.__class__.__name__ + ")")
        return 0
    items = fields.get("results", fields) if isinstance(fields, dict) else fields
    filled = []
    for item in items or []:
        label = ((item.get("custom_field") or {}).get("label")) or item.get("label")
        value = item.get("value")
        if label and value not in (None, "", []):
            filled.append(f"{label} = {value}")
    print("custom fields:", "; ".join(filled) or "(none filled)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("convert", help="import layout -> upload layout (writes a CSV, nothing else)")
    c.add_argument("src", help="the import CSV (from photo-import, pdf-import or the enrichment)")
    c.add_argument("--out", help="file name for the upload CSV (default datasift_upload_<time>.csv)")
    c.add_argument("--out-dir", help="folder for the upload CSV (default the output folder)")
    r = sub.add_parser("readback", help="read one record back over the Open API key (read only)")
    r.add_argument("--street", required=True, help="the street line as DataSift stores it")
    r.add_argument("--zip", required=True, dest="zip_code", help="the five-digit ZIP")
    a = ap.parse_args()
    if a.cmd == "convert":
        return convert(a.src, a.out, a.out_dir)
    return readback(a.street, a.zip_code)


if __name__ == "__main__":
    sys.exit(main())
