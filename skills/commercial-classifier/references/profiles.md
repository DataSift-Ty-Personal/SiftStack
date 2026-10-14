# Source profiles and fields

`cc inspect <file>` detects the source from its headers and maps each canonical field. Override any
mapping with `--map field=Header` (repeatable; header matching ignores case and punctuation).

## Profiles

| Profile | Detected by | Notes |
|---|---|---|
| `fa` | `landusecode`, `countylandusecode`, `propertyclassid`, `storiesnbrcode`, ... | National code table built in. Stories are stored x100 and quality, condition and construction are coded; the profile converts them |
| `datasift` | `exported from REISift.io`, `Structure type`, `Building use code`, `Storeys` | Structure type resolves through the national table. Business Name is the owner company |
| `regrid` | `parcelnumb`, `usecode`, `usedesc`, `lbcs_*`, `mailadd`, `ll_gisacre` | `usecode` is county-native and is labelled per county |
| `loopnet_crexi` | `Property Type`, `Property Subtype`, `Building Size`, `Cap Rate` | Vendor types already match the taxonomy words |
| `reonomy` | `reonomy_id`, `asset_category`, `asset_type`, `reported_owner` | |
| `arcgis` | `OBJECTID`, `Shape_Area`, `GlobalID` (any layer from `fetch-parcels`) | Field aliases from `<file>.fields.json` count as extra header names |
| `generic` | anything else | Broad synonym list; county codes are learned |

Force one with `--profile <name>`.

## Canonical fields

| Field | Feeds |
|---|---|
| `apn`, `apn_seq`, `fips`, `county` | the row id and the county key (codes are learned per county) |
| `situs_address`, `situs_city`, `situs_state`, `situs_zip` | output, mall-outparcel rule, out-of-state test |
| `lat`, `lon` | business-point matching (a polygon's centroid fills these when missing) |
| `geometry` | parcel polygon (WKT, GeoJSON or ArcGIS rings) for true point-in-polygon |
| `use_code`, `use_desc` | the main land-use signal |
| `county_use_code`, `county_use_desc` | a local code under a national one (refines generic codes) |
| `structure_type`, `vendor_type`, `vendor_subtype`, `lbcs_code`, `lbcs_desc` | other use signals |
| `property_class`, `zoning`, `zoning_desc` | coarse signals, capped at 0.5 so a specific use code always wins |
| `business_name` | a tenant or business on the parcel |
| `legal_desc`, `subdivision` | condo and fuel keyword rules |
| `building_sqft`, `stories`, `units_res`, `units_comm`, `year_built`, `lot_acres` | size, unit and physics rules, grades |
| `quality`, `condition`, `construction` | building grade |
| `mv_total`, `mv_land`, `mv_impr`, `assessed_total`, `vendor_avm` | value estimates |
| `sale_price`, `sale_date` | calibration and recent-sale values |
| `owner1` to `owner4`, `owner_first`, `owner_last`, `owner_is_corp` | true-owner resolution |
| `mail_address`, `mail_city`, `mail_state`, `mail_zip` | owner linking and out-of-state flags |

Extra mapping-only fields: `lot_sqft` (converted to acres), `situs_house` + `situs_street`
(joined when there is no full address).

## Things the ingest does for you

- Money like `$1,234` and dates in any common format (including ArcGIS epoch milliseconds) parse.
- A blank cell stays blank. It is never read as zero.
- A lot-size column whose values look like square feet is converted to acres.
- A 0,0 or out-of-range coordinate is dropped.
- Exact duplicate rows are dropped and counted; two different rows for one parcel both stay.

## Options

| Option | Use |
|---|---|
| `--state XX` | situs state when the file has none (out-of-state owner flags need it) |
| `--county-key NAME` | a county label when the file has no county or FIPS column |
| `--sheet NAME` | worksheet in an .xlsx |
| `--crosswalk file.csv` | load an extra crosswalk (for example one saved from another run of the same county) |
| `--places file.csv` | your own business points instead of Overture |
| `--no-evidence` | skip the business-points pass |
| `--recent-since YYYY-MM-DD` | what counts as a recent sale (default: 4 years before the newest sale in the file) |
