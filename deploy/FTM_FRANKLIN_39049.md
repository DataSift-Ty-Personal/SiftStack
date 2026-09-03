# First-to-Market Investigation - Franklin County, OH (FIPS 39049)

Written 2026-08-19 for the ty+1 staging build-out. Data: County List Playbook
shard (learn.datasift.ai/county-data/39.json, key 39049) plus the
39049-Franklin-OH-doors-per-deal.xlsx First to Market sheet. Companion to the
SiftMap pull driver `src/staging_build_39049.py`.

## 1. Why FTM matters here

County window 2026-01 to 2026-06: 1,650 investor purchases, baseline 183.5
doors per deal across 302,731 SFR supply, median gross spread $51,000.

Ohio is a JUDICIAL foreclosure state. Two consequences:
- The provider's Notice of Default / Notice of Foreclosure feeds are
  near-empty churn artifacts (a 141x lift on a 20-record list). They are
  excluded from the SiftMap build on purpose. The real early distress signal
  is court-side: lis pendens and foreclosure complaints at the Clerk of
  Courts, then Final Judgment.
- The first public signal for most distress in this county is a COURT FILING,
  which is exactly the data SiftMap does not carry here.

## 2. Provider coverage vs the gap

Per the shard ftm[] block:

| Notice type | SiftMap status | List size | Deals (6mo) |
|---|---|---|---|
| Foreclosure (lis pendens track) | covered | 264 | 69 |
| Tax delinquent | covered | 11,123 | 133 |
| Probate | covered | 6,623 | 76 |
| Tax sale | NO PROVIDER | - | - |
| Eviction | NO PROVIDER | - | - |
| Code violation | NO PROVIDER | - | - |
| Divorce | NO PROVIDER | - | - |

The workbook's framing: the four no-provider types are proven deal-makers
nationally and nobody can buy them here, so county-direct pulls are not a
backup, they are the only way in. Obituary supply for the county: 1,159
records (Expert plan data list, worked separately).

## 3. Sources (pull first / next / last, from the playbook page)

### A. Pull first

1. **Franklin County Clerk of Courts, General Division** (lis pendens +
   foreclosure complaints). 614-525-3621.
   https://fcdcfcjs.co.franklin.oh.us/CaseInformationOnline/
   369 S High St, 3rd Fl, Columbus OH 43215. Daily cadence, verified. The
   portal is a convenience front end; official records are at the courthouse.
   No signup for search; likely bot protection to evaluate.
2. **Franklin County Recorder** (deeds, mortgages, mechanic's / IRS / state
   tax / HOA / child-support liens, lis pendens). 614-525-3930.
   https://franklin.oh.publicsearch.us/  373 S High St, 18th Fl. Daily,
   verified, rated Easy. publicsearch.us is a standard vendor UI; check for a
   JSON search endpoint behind it (same reverse-engineering pattern as the
   Knox ROD paxsub /api/v2Search work). Tax and child-support liens index
   against the PERSON, so a name-to-parcel join against the county parcel
   roll is required (same guards as the Knox lien join: ~40% expected hit
   rate at full volume).
3. **Franklin County Probate Court**. 614-525-3894.
   https://probate.franklincountyohio.gov/Home  373 S High St, 22nd Fl.
   probate@franklincountyohio.gov. Daily, Easy. Contact is the executor or
   administrator, never the deceased. Probate is provider-covered in SiftMap
   here, so the court pull is an FTM speed edge (days vs provider lag), not
   the only route.
4. **Franklin County Treasurer, Tax Lien Sale**. 614-525-3438.
   https://treasurer.franklincountyohio.gov/Delinquent-Taxes/Tax-Lien-Sale
   373 S High St, 1st Fl. Annually Oct/Nov. Certificates are bundled and sold
   as ONE portfolio, so the sale itself is not buyable; the value is the
   pre-sale delinquent parcel list.
5. **Treasurer pre-sale delinquency roll**. Same office, ongoing. No
   published URL; call the Delinquent Tax Department. This is the tax_sale
   feed the shard flags as no-provider.
6. **City tax delinquency**. Rated Hard. No county-level portal; must be
   researched per incorporated city (Columbus, Dublin, Westerville, ...).

### B. Pull next (code enforcement)

- **Columbus Dept. of Building & Zoning Services** code enforcement. Many OH
  cities run Accela Citizen Access portals; the SiftStack Accela patterns
  from Knox (reference_knox_public_portals) should transfer. May require a
  public records request for bulk data.
- **Franklin County Economic Development & Planning** for unincorporated
  county code violations. Records lag; usually a records request.
- Condemned / unsafe structures / demolition lists (city).

### C. Pull last (evictions)

- **Franklin County Municipal Court Clerk** eviction docket. 614-645-8186.
  https://www.fcmcclerk.com/case/search  375 S High St. Daily, verified.
  Search by plaintiff name; 2+ filings by the same landlord in 12 months is
  the tired-landlord signal. The tenant is not the lead; the LANDLORD is.

## 4. The urgency rule (learned on Knox, applies here)

Evictions and code violations accumulate FORWARD or not at all: courts keep
roughly the current week on the server and cities overwrite agenda PDFs.
Every week not collecting is a week of first-to-market data that never comes
back. These two collectors should be stood up first even if records are just
banked to CSV before the CRM flow exists.

## 5. What to build in SiftStack (later pass, not this one)

Reuse the existing shapes:
- Collector per source writing scratch JSON/CSV (the `knox_ftm_pull.py`
  collector pattern; its aggregation/buy-box/upload layer already exists).
- Name-to-parcel join for person-indexed liens: port `knox_lien_resolve.py`
  guards to the Franklin parcel roll. Franklin owner roll is FREE:
  apps.franklincountyauditor.com `/Parcel_CSV/{yyyy}/{mm}/Parcel.csv` carries
  NAME1/2/3 + mailing + values (the newer-looking Outside_User_Files extract
  has NO owner fields; the folder path is stale on purpose, trust the
  Last-Modified header). Already documented in docs/api/county-data.md and
  the soi_* pipeline pulls it today.
- Upload via `datasift_api_upload.py` (minted JWT, custom fields, tags as
  arrays, option UUIDs) into the ty+1 lists, tagged `Courthouse Data` so the
  niche sequential presets catch them.
- Buy box: same $1-700k SFR off-market box as the SiftMap pulls.

## 6. What Ty needs to sign up for / decide

| Item | Needed for | Cost/effort |
|---|---|---|
| Clerk of Courts portal account (if bulk search requires login) | lis pendens daily pull | free, verify |
| publicsearch.us access check (anonymous search limits, captcha) | Recorder liens | free, verify |
| Treasurer Delinquent Tax Dept contact (request the pre-sale roll) | tax_sale list | phone call, possibly records request |
| Columbus code enforcement records request (or Accela account) | code violations | free to low |
| fcmcclerk.com search automation check (bot wall?) | evictions | free, verify |
| Scrapfly (already licensed) for any Cloudflare-walled portal | fallback fetcher | existing key |
| Skip trace + phone scoring (Tracerfy + Trestle, existing keys) | working the lists | existing |

No paid data vendor is required for any Franklin FTM source identified.

## 7. Where to aim it (targeting tables from the shard)

Top ZIPs by stars (deals / dpd / median gross / momentum):

| ZIP | Stars | Deals | DPD | Med gross | Momentum | Local signal edge |
|---|---|---|---|---|---|---|
| 43207 | 5 | 120 | 121.7 | $67,000 | 1.71 | High Equity 57% share, 1.31x |
| 43206 | 5 | 77 | 89.9 | $54,000 | 1.68 | OOS 1.59x, Tired LL 1.34x |
| 43224 | 5 | 73 | 142.9 | $63,500 | 1.36 | Tax Delinquent 23% share, 2.88x |
| 43229 | 5 | 48 | - | $70,000 | 1.64 | Tax Del 1.54x, High Equity 1.35x |
| 43204 | 4 | 104 | 108.9 | $53,500 | 1.17 | Tired Landlord 1.36x |
| 43211 | 4 | 96 | 81.0 | $53,000 | 1.20 | Vacant 2.88x, 68% AI-high share |

Price bands (deal density concentrates under $300k):

| AVM band | Deals | Supply | DPD |
|---|---|---|---|
| under $150k | 211 | 12,452 | 59.0 |
| $150k-$200k | 317 | 32,545 | 102.7 |
| $200k-$250k | 315 | 40,467 | 128.5 |
| $250k-$300k | 225 | 39,678 | 176.3 |
| $300k-$400k | 285 | 71,035 | 249.2 |
| $400k-$500k | 129 | 46,094 | 357.3 |
| $500k+ | 164 | 63,854 | 389.4 |

Build order recommendation: evictions + code violations first (forward-only
accumulation), Recorder liens second (biggest verified daily feed), Treasurer
pre-sale roll third (annual timing, request early), Clerk lis pendens as the
judicial-state foreclosure backbone alongside the SiftMap is_lis_pendens
feeder already built.
