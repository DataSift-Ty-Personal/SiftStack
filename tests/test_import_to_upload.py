"""import_to_upload convert: an import CSV becomes a CSV the key uploader reads. Zero network.

Run: python tests/test_import_to_upload.py
"""
import csv
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"


def run(args, env):
    return subprocess.run([sys.executable, *args], cwd=ROOT, env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="import_to_upload_"))
    env = dict(os.environ, SIFTSTACK_OUTPUT_DIR=str(tmp), PYTHONPATH=str(SRC), PYTHONIOENCODING="utf-8")
    make = (
        "from notice_parser import NoticeData\n"
        "from data_formatter import write_csv\n"
        "n = NoticeData(address='123 Example St', city='Sampletown', state='TN', zip='37000',\n"
        "               owner_name='Jordan Sample', notice_type='probate', county='Example',\n"
        "               date_added='2026-11-24', date_published='2026-11-20')\n"
        "print(write_csv([n], filename='intake.csv'))\n"
    )
    r = run(["-c", make], env)
    assert r.returncode == 0, r.stderr[-800:]
    intake = Path(r.stdout.strip().splitlines()[-1])
    assert intake.is_file(), intake

    out_dir = tmp / "chain"
    r = run([str(SRC / "import_to_upload.py"), "convert", str(intake), "--out", "upload.csv",
             "--out-dir", str(out_dir)], env)
    assert r.returncode == 0, (r.stdout + r.stderr)[-800:]
    upload = out_dir / "upload.csv"
    assert upload.is_file(), r.stdout
    rows = list(csv.DictReader(open(upload, encoding="utf-8")))
    assert len(rows) == 1, rows
    row = rows[0]
    assert row.get("Property Street Address") == "123 Example St", row
    assert row.get("Property ZIP Code") == "37000", row
    assert "Probate" in (row.get("Lists") or ""), row
    assert "next: python src/datasift_api_upload.py --csv" in r.stdout

    r = run([str(SRC / "import_to_upload.py"), "convert", str(tmp / "missing.csv")], env)
    assert r.returncode == 2 and "No such file" in r.stdout, r.stdout

    for p in (SRC / "import_to_upload.py", Path(__file__)):
        text = p.read_text(encoding="utf-8")
        dashes = (chr(0x2014), chr(0x2013))
        assert not any(d in text for d in dashes), p
    print("import_to_upload: 4 checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
