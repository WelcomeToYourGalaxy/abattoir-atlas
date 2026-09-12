# Deduplication report

Generated 2026-09-12

- Input rows: **30,832**
- Facilities after clustering: **26,813**
- Rows absorbed into a multi-source facility: **4,019**
- Flagged for review, not merged: **543 pairs**

## Rows per source

- EU / EFTA member states: 14,372
- Non-EU (EU-approved): 9,148
- USDA FSIS: 7,241
- China CIFER: 71

## What joined the merges

- `id`: 621 clusters
- `addr`: 359 clusters
- `geo`: 92 clusters

## Cluster sizes

- 1 source record: 25,770 facilities
- 2 source records: 959 facilities
- 3 source records: 65 facilities
- 4 source records: 13 facilities
- 5 source records: 4 facilities
- 6 source records: 1 facilities
- 20 source records: 1 facilities

Nothing was discarded. Every input row is inside exactly one facility above, and `out/facilities.json` carries all of them verbatim.
