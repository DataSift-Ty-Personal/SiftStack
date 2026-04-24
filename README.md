# SiftStack

Full-stack real estate investing operations platform built for [DataSift.ai](https://datasift.ai). Scrapes distress-property notices from public portals, runs a 10-step enrichment pipeline (address standardization, Zillow data, obituary research, skip trace, phone scoring), and pushes clean records into DataSift every morning — ready for niche sequential marketing.

**Target market:** Ohio — Franklin (Columbus), Montgomery (Dayton), Greene (Xenia) counties.
**Notice types:** foreclosure + probate (Phase 1); tax sales, tax delinquency, evictions, code violations planned.
**Requires a DataSift.ai account.**

## What It Does

```
  Sheriff Sale Portals       ──┐
  Probate Court Portals      ──┤
  PDF Tax Sale Lists (OCR)   ──┼──→  Enrichment Pipeline  ──→  DataSift Upload  ──→  Niche Sequential
  Dropbox Courthouse Photos  ──┤     (10 steps)                (automated)          Marketing
  CSV Re-Import              ──┘
```

Every input path produces the same `NoticeData` records and flows through the same enrichment pipeline.

### OH Data Sources (Phase 1 scope)

| County | Notice Type | Source | Tech | Account? |
|---|---|---|---|---|
| Franklin | Probate | [probate.franklincountyohio.gov](https://probate.franklincountyohio.gov/record-search/general-case-index) | Custom .NET | No |
| Franklin | Foreclosure | [franklin.sheriffsaleauction.ohio.gov](https://franklin.sheriffsaleauction.ohio.gov) | RealAuction | Free account |
| Montgomery | Probate | [go.mcohio.org](https://go.mcohio.org/applications/probate/prodcfm/casesearchall.cfm) | ColdFusion | No |
| Montgomery | Foreclosure | [go.mcohio.org](https://go.mcohio.org/applications/sheriffauction/sflistauction.cfm) | ColdFusion | No |
| Greene | Probate | [courts.greenecountyohio.gov/probatejw](https://courts.greenecountyohio.gov/probatejw) | JWorks | No |
| Greene | Foreclosure | [apps.greenecountyohio.gov/sheriff/sheriffsales.aspx](https://apps.greenecountyohio.gov/sheriff/sheriffsales.aspx) | ASP.NET | No |

### Enrichment Pipeline (10 steps)

1. Deduplicate by address
2. Vacant land filter
3. Entity filter (LLC/Corp/Trust research)
4. Probate property lookup (Auditor → executor family → people search)
5. Tax delinquency (county Auditor — Phase 4+)
6. Address standardization (Smarty USPS + ZIP+4 + geocode + vacancy)
7. Commercial filter (Smarty RDI)
8. Zillow enrichment (Zestimate, MLS status, equity, beds/baths)
9. Obituary search (DOD, heirs, decision-maker ranking, DOD sanity check)
10. Data validation (mailable flag)

### DataSift Automation

After enrichment, records are automatically:
- Formatted into 41-column DataSift CSV with tags, lists, and custom fields
- Uploaded via Playwright browser automation (5-step wizard)
- Enriched with SiftMap property data inside DataSift
- Skip traced for phones + emails (DataSift unlimited plan)
- Routed into DataSift's niche sequential campaigns (21 filter presets, 26 TCA sequences)

## Quick Start

```bash
# Install
pip install -r requirements.txt
playwright install chromium

# Configure
cp .env.example .env
# Edit .env — fill in DataSift login + enrichment API keys (+ RealAuction if scraping Franklin)

# Run
python src/main.py daily                                          # new notices since last run
python src/main.py daily --counties Franklin                      # only Franklin
python src/main.py daily --types probate                          # only probate
python src/main.py daily --upload-datasift                        # + DataSift upload/enrich/skip trace
python src/main.py daily --upload-datasift --notify-slack         # full white-glove daily run
```

See [CLAUDE.md](CLAUDE.md) for full operational docs, daily cadence, DataSift conventions, and troubleshooting.

## Apify Deployment

Runs in the cloud on a daily schedule — no need to keep your laptop on.

```bash
npm install -g apify-cli
apify login
apify push
```

Daily schedule configured in Apify Console. Outputs: Apify Dataset, key-value store CSV, DataSift upload, Slack summary.

## License

See LICENSE.
