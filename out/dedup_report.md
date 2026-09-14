# Deduplication report

Generated 2026-09-14

- Input rows: **158,165**
- Facilities after clustering: **107,096**
- Rows absorbed into a multi-source facility: **51,069**
- Flagged for review, not merged: **39,374 pairs**

## Rows per source

- Farm Transparency: 113,351
- EU / EFTA member states: 22,112
- Non-EU (EU-approved): 15,390
- USDA FSIS: 7,241
- China CIFER: 71

## What joined the merges

- `addr`: 2,558 clusters
- `geo`: 1,758 clusters
- `id`: 1,276 clusters

## Cluster sizes

- 1 source record: 101,736 facilities
- 2 source records: 4,628 facilities
- 3 source records: 516 facilities
- 4 source records: 114 facilities
- 5 source records: 58 facilities
- 6 source records: 10 facilities
- 7 source records: 4 facilities
- 8 source records: 4 facilities
- 9 source records: 2 facilities
- 10 source records: 1 facilities
- 13 source records: 1 facilities
- 20 source records: 1 facilities
- 22 source records: 1 facilities
- 33 source records: 20 facilities

Nothing was discarded. Every input row is inside exactly one facility above, and `out/facilities.json` carries all of them verbatim.
