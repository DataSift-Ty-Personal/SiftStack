# Where to get parcel data

The classifier takes any file with at least one column that says what a property is (a land-use
code or description, a structure or property type, zoning, a property class, or a business name).
More columns make more of the pipeline work:

| You have | You get |
|---|---|
| a use code or description | asset class and sub-type |
| latitude/longitude, or polygons | business-point confirmation (polygons can also correct) |
| market or assessed value plus sale price and date | value bands calibrated from that county's own sales |
| owner name and mailing address | true-owner roll-up, portfolios, out-of-state flags |
| building size, units, stories | the size, unit and physics rules |

## Free: the county's own ArcGIS parcel layer (best starting point)

Most US counties publish parcels as a public ArcGIS layer with owner, land use, values and often
the last sales.

1. Search the county's GIS or open data site for "parcels" (often `gis.<county>.gov`,
   `<county>-<state>.opendata.arcgis.com`, or the auditor/assessor/property appraiser site).
2. Open the layer's "API" or "View in ArcGIS REST" link. You want a URL ending in
   `/FeatureServer/<n>` or `/MapServer/<n>`. A REST directory (`.../arcgis/rest/services`) lets
   you browse to it.
3. `python scripts/cc.py probe-layer "<url>"` shows the fields and what the classifier will use.
   If there is no land-use or class field, look for a second assessor layer or table on the same
   server; some counties split geometry and attributes.
4. `python scripts/cc.py fetch-parcels "<url>" --out parcels.csv`

Notes: some layers cap pages at 1,000 to 2,000 rows (the downloader pages through it). A few
need a token; those are not public, so use the county's bulk download instead. `--where` filters
on the server, e.g. `--where "CLASS='C'"` for commercial only.

## Free: county bulk downloads

Many assessors post a full CSV or a nightly CAMA extract (a zip of tables joined on a parcel id).
Join the owner and land-use tables yourself into one CSV keyed by parcel, then classify it.

Statewide parcel layers often strip the owner fields from the public view. They still classify
by land use; they just cannot do the owner roll-up.

## Free: business points

`cc run` pulls Overture Maps Places for the area automatically (free, no key; `pip install duckdb`
makes it fast) and falls back to OpenStreetMap. A city or county business-license export with
name, category, latitude and longitude can be passed with `--places file.csv`.

## A DataSift export

Filter the records in DataSift, export to CSV, and run it as-is. The export's "Structure type" is
national land-use text that resolves straight to the built-in table, so no labelling is needed.
"Business Name" is read as the OWNER company, which is what it is in a DataSift export.

## Paid sources (if you already have them)

| Source | Notes |
|---|---|
| Regrid | Detected automatically. `usecode` is county-native, so it is labelled per county; `lbcs_*` and `usedesc` help |
| PropStream, ATTOM, CoreLogic exports | Run as generic; check `cc inspect` and add `--map` where a column was missed |
| LoopNet, Crexi, CoStar listing exports | Their property type already uses this taxonomy's words |
| Reonomy | Its asset category and type read as the vendor's own type |
| First American assessor files | Detected automatically; built-in national code table, no labelling |

Paid data stays wherever its license says it stays. The classifier runs locally and sends
nothing anywhere except the public business-points request (which sends only a bounding box).
