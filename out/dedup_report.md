# Deduplication report

Generated 2026-09-12

- Input rows: **20,431**
- Facilities after clustering: **19,724**
- Rows absorbed into a multi-source facility: **707**
- Flagged for review, not merged: **229 pairs**

## Rows per source

- EU third-country list: 13,119
- USDA FSIS: 7,241
- China CIFER: 71

## What joined the merges

- `addr`: 305 clusters
- `geo`: 92 clusters
- `id`: 86 clusters

## Cluster sizes

- 1 source record: 19,248 facilities
- 2 source records: 450 facilities
- 3 source records: 23 facilities
- 4 source records: 2 facilities
- 5 source records: 1 facilities

Nothing was discarded. Every input row is inside exactly one facility above, and `out/facilities.json` carries all of them verbatim.
