"""Build a tiny 2-row upload CSV from TODAY's master for the Notice-Screenshot
field test: 2527 Tech Dr + 135 Ridge Rd, each tagged with its permanent Dropbox
auction-notice link. Verifies the column lands in the reisift custom field.
"""
import csv
import glob
from pathlib import Path

# permanent Dropbox ?raw=1 links already created for these properties' notices
URLS = {
    "2527 tech": "https://www.dropbox.com/scl/fi/xdom3cshuo7ggplqr0y3r/notice_540224.png?rlkey=ap90ulwrt9fqjr5utvclid3bt&raw=1",
    "135 ridge": "https://www.dropbox.com/scl/fi/50lusjohdta2gt8icw5xa/notice_540223.png?rlkey=gwxy12dnil3sog0i4ep1di1h1&raw=1",
}

masters = sorted(g for g in glob.glob("output/foreclosure_master_active_*.csv") if "_with_screenshots" not in g and "_sfr" not in g)
master = masters[-1]
rows = list(csv.DictReader(open(master, encoding="utf-8")))
print("master:", master, "| rows:", len(rows))

sel = []
for r in rows:
    addr = (r.get("address") or "").strip().lower()
    for key, url in URLS.items():
        parts = key.split()
        if all(p in addr for p in parts):
            r["notice_screenshot_url"] = url
            sel.append(r)
            break

cols = list(rows[0].keys())
if "notice_screenshot_url" not in cols:
    cols.append("notice_screenshot_url")
out = "output/_verify_ss.csv"
with open(out, "w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(sel)

print(f"wrote {len(sel)} rows -> {out}")
for r in sel:
    print(f"  {r.get('address')} | county={r.get('county')} | auction={r.get('auction_date')} | url={'yes' if r.get('notice_screenshot_url') else 'NO'}")
