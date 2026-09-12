# Deduplication report

Generated 2026-09-12

- Input rows: **24,994**
- Facilities after clustering: **22,880**
- Rows absorbed into a multi-source facility: **2,114**
- Flagged for review, not merged: **470 pairs**

## Rows per source

- Non-EU (EU-approved): 14,673
- USDA FSIS: 7,241
- EU / EFTA member states: 3,009
- China CIFER: 71

## What joined the merges

- `id`: 418 clusters
- `addr`: 336 clusters
- `geo`: 92 clusters

## Cluster sizes

- 1 source record: 22,061 facilities
- 2 source records: 753 facilities
- 3 source records: 49 facilities
- 4 source records: 11 facilities
- 5 source records: 5 facilities
- 6 source records: 1 facilities

Nothing was discarded. Every input row is inside exactly one facility above, and `out/facilities.json` carries all of them verbatim.
