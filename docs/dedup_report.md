# Deduplication report

Generated 2026-09-12

- Input rows: **44,814**
- Facilities after clustering: **36,855**
- Rows absorbed into a multi-source facility: **7,959**
- Flagged for review, not merged: **1,049 pairs**

## Rows per source

- EU / EFTA member states: 22,112
- Non-EU (EU-approved): 15,390
- USDA FSIS: 7,241
- China CIFER: 71

## What joined the merges

- `id`: 1,280 clusters
- `addr`: 464 clusters
- `geo`: 92 clusters

## Cluster sizes

- 1 source record: 35,059 facilities
- 2 source records: 1,645 facilities
- 3 source records: 117 facilities
- 4 source records: 23 facilities
- 5 source records: 7 facilities
- 6 source records: 1 facilities
- 8 source records: 2 facilities
- 20 source records: 1 facilities

Nothing was discarded. Every input row is inside exactly one facility above, and `out/facilities.json` carries all of them verbatim.
