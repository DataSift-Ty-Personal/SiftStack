# Upload layout, intake columns and the read-back

Load this at step 3 (shaping the upload file) and step 4 (reading the first record back). Every command runs from
the SiftStack folder.

## The upload file

`python src/datasift_api_upload.py` reads these columns by their exact names. Any other column in the file is
ignored, so the extra columns the formatter writes do no harm.

| Column | What goes in it | Notes |
|---|---|---|
| Property Street Address | the house's street line, house number first | required; a row without one fails |
| Property City | the house's city | |
| Property State | two letters | a blank becomes TN, so fill it outside Tennessee |
| Property ZIP Code | five digits | |
| Owner First Name, Owner Last Name | the contact: the owner; the landlord (plaintiff) on an eviction; the petitioner on a divorce; the personal representative on a probate | a company goes in Owner Last Name with Owner First Name blank and is sent as a company; both blank fails the row |
| Mailing Street Address, Mailing City, Mailing State, Mailing ZIP Code | the contact's own mailing address | blank falls back to the property, which is wrong for most landlords and many personal representatives |
| Lists | one list title, for example `Eviction` | DataSift makes the list the first time a title is used |
| Tags | a comma list | the uploader sends them as separate tags |
| Notes | free text | posted once on the record's message board |
| Notice Type | foreclosure, probate, tax_sale, tax_delinquent, eviction, code_violation or divorce | a select field: the option must exist in your account |
| County | your county's name | a select field: add your county as an option first |
| Date Added | M/D/YYYY or YYYY-MM-DD | on notice uploads, the day you added the record |
| Foreclosure Date | the sale date | the hot auction list depends on it |
| Tax Auction Date | a tax sale's sale date | |
| Personal Representative | probate only | |
| Decedent Name | probate only | |
| Source URL | where the record came from | |
| Tax Deliquent Value | delinquent tax owed | the column keeps DataSift's own spelling |

The Knox county pull also writes Outstanding Lien Amount, Liens Active vs Total, Lien Type, Total Delinquency, Est
Equity, Debt vs Equity Pct, Upside Down and Upside Down Reason.

A custom field or a select option the account does not have is skipped and listed under WARNINGS at the end of the
run. It is never guessed. Fix the field or option in the app and send the rows again: a second send attaches to the
same records.

## Tags and lists, by record type

The formatter (`src/datasift_formatter.py`) writes these. Foreclosures and probates carry the tag FTM, which is what
the foreclosure and estate lane presets read. The other county record types carry Courthouse Data without FTM.

| Record type | List | Tags |
|---|---|---|
| foreclosure | Foreclosure | Courthouse Data, FTM, foreclosure, the county (lowercase), the month of the notice (YYYY-MM), living or deceased, has_auction while the sale is ahead |
| probate | Probate | Courthouse Data, FTM, probate, the county, the month |
| tax_sale | Tax Sale | Courthouse Data, tax_sale, the county, the month |
| tax_delinquent | Tax Delinquent | Courthouse Data, tax_delinquent, the county, the month |
| eviction | Eviction | Courthouse Data, eviction, the county, the month |
| code_violation | Code Violation | Courthouse Data, code_violation, the county, the month |
| divorce | Divorce | Courthouse Data, divorce, the county, the month |
| lien (Knox county pull) | Liens | Courthouse Data, lien, the county, the source, the filing quarter (`filed_<YYYY-Qn>`), a lien amount band, `pulled_<date>` |
| condemnation (Knox county pull) | Condemned | Courthouse Data, code_violation, the county, the source, `filed_<YYYY-Qn>`, `pulled_<date>` |

Photo imports also carry `photo_import`. A record with a tax balance carries `tax_delinquent`.

## Intake columns for csv-import

`python src/main.py csv-import --csv-path <file> --csv-county <County>` reads a CSV whose headers are these names.
Rename an export's headers to match before you run it. Unknown headers are ignored.

| Column | What goes in it |
|---|---|
| address | the property's street line |
| city, state, zip | the property's city, two-letter state (always fill it: a blank becomes TN) and ZIP |
| owner_name (or full_name) | the contact as one string: the owner, the landlord on an eviction, the personal representative on a probate |
| owner_street, owner_city, owner_state, owner_zip | the contact's mailing address |
| notice_type | one of the seven record types |
| county | the county name |
| date_published | the filing or publication date (YYYY-MM-DD or M/D/YYYY) |
| auction_date | the sale date on a foreclosure or tax sale |
| decedent_name | probate only |
| parcel_id | when the source shows it |
| source_url | the page or file the record came from |

A row without an address, a city and a ZIP is dropped at validation and named in the log.

## Turn an import into the upload file

The photo, PDF and csv-import runs write the import format (lowercase columns), which the uploader cannot read.
One command turns one into the other, in Bash or PowerShell alike:

```bash
python src/import_to_upload.py convert output/first-to-market-chain/intake/<import>.csv --out upload_<type>_<date>.csv --out-dir output/first-to-market-chain
```

It prints the path it wrote, `output/first-to-market-chain/upload_<type>_<date>.csv`, and the dry-run command to run
next. It writes nothing to DataSift. The Tennessee notice pull and the Knox county pull already write the upload
layout, so they skip this.

## Read one record back

After `--limit 1 --commit`, read the record over the key and compare it with the row you sent:

```bash
python src/import_to_upload.py readback --street "123 Example St" --zip 12345
```

It prints the record, its owner, lists, tags, notes and every custom field that holds a value. It only reads.

Write the street the way DataSift stores it (it shortens Street to St, Road to Rd). The search only finds records
DataSift files as complete, so a company-owned record can read "not found": open it in the app instead. Then open
the record in the app and check the custom fields: Notice Type, County, Date Added, and the sale date on a
foreclosure.
