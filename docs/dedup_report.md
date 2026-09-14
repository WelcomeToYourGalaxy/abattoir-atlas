# Deduplication report

Generated 2026-09-14

- Input rows: **149,201**
- Facilities after clustering: **134,826**
- Rows absorbed into a multi-source facility: **14,375**
- Flagged for review, not merged: **39,686 pairs**

## Rows per source

- Farm Transparency: 75,282
- EU animal health: 29,105
- EU / EFTA member states: 22,112
- Non-EU (EU-approved): 15,390
- USDA FSIS: 7,241
- China CIFER: 71

## What joined the merges

- `addr`: 3,416 clusters
- `geo`: 1,762 clusters
- `id`: 1,363 clusters

## Cluster sizes

- 1 source record: 128,542 facilities
- 2 source records: 5,449 facilities
- 3 source records: 594 facilities
- 4 source records: 135 facilities
- 5 source records: 59 facilities
- 6 source records: 11 facilities
- 7 source records: 4 facilities
- 8 source records: 4 facilities
- 9 source records: 3 facilities
- 10 source records: 1 facilities
- 13 source records: 1 facilities
- 20 source records: 1 facilities
- 21 source records: 1 facilities
- 22 source records: 1 facilities
- 33 source records: 20 facilities

Nothing was discarded. Every input row is inside exactly one facility above, and `out/facilities.json` carries all of them verbatim.
