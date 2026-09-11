# Deduplication report

Generated 2026-09-11

- Input rows: **7,241**
- Facilities after clustering: **7,016**
- Rows absorbed into a multi-source facility: **225**
- Flagged for review, not merged: **191 pairs**

## Rows per source

- USDA FSIS: 7,241

## What joined the merges

- `addr`: 122 clusters
- `geo`: 92 clusters

## Cluster sizes

- 1 source record: 6,802 facilities
- 2 source records: 206 facilities
- 3 source records: 6 facilities
- 4 source records: 1 facilities
- 5 source records: 1 facilities

Nothing was discarded. Every input row is inside exactly one facility above, and `out/facilities.json` carries all of them verbatim.
