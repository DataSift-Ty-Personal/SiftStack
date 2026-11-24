"""The curative title engine: find the houses where the owner of record died and the title is stuck, rank them,
research every heir, check whose phone each number is, and hand the work-now houses to the loader.

    python src/curative_title.py --help

Every step reads the user's OWN account over the DataSift Open API key, free public county sources where a
county adapter exists (Knox and Blount County, Tennessee are built in), and the vendors the user holds keys for
(SmartSkip, Enformion, Trestle). Nothing is written to DataSift here: the loader
(src/agents/autopilot/curative_setup.py) does that, after a person has read the results.

Modules:
    common     names, dates, money and the work folder
    counties   the county sources: tax roll, city tax, obituary index, probate index (by FIPS)
    account    the user's own records and their SiftMap detail (and `widen`: pull more houses in)
    assemble   one row per house from everything read
    deaths     an owner matched to an obituary index row (age and date checked)
    score      the 105-point score, the play and the tier
    family     SmartSkip family trees (dry quote first; paid only with --commit)
    stories    the heir research: who inherits, each share, and a one-page story per house
    phones     Trestle Real Contact: which number belongs to which heir
    workbook   the ranked workbook
"""

VERSION = "1.0"
