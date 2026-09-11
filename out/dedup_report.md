# Deduplication report

Generated 2026-09-11

- Input rows: **17,101**
- Facilities after clustering: **16,642**
- Rows absorbed into a multi-source facility: **459**
- Flagged for review, not merged: **216 pairs**

## Rows per source

- EU third-country list: 9,789
- USDA FSIS: 7,241
- China CIFER: 71

## What joined the merges

- `addr`: 163 clusters
- `geo`: 92 clusters
- `id`: 63 clusters

## Cluster sizes

- 1 source record: 16,328 facilities
- 2 source records: 298 facilities
- 3 source records: 14 facilities
- 4 source records: 1 facilities
- 5 source records: 1 facilities

Nothing was discarded. Every input row is inside exactly one facility above, and `out/facilities.json` carries all of them verbatim.
