"""Commercial property classifier.

Any parcel or property file in (a county assessor export, a DataSift export,
Regrid, PropStream, ATTOM, LoopNet/Crexi, an ArcGIS layer, First American),
asset class and sub-type out, with confidence, provenance, business-point
evidence, value bands, true-owner grouping and a review queue.

The core runs on the Python standard library. openpyxl (Excel), pyarrow
(parquet), duckdb (Overture business points) and shapely (faster polygon
math) are used when present and skipped with a stated reason when not.
"""

VERSION = "1.0.0"
