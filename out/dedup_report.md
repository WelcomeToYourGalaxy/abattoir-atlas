# Deduplication report

Generated 2026-09-13

- Input rows: **82,883**
- Facilities after clustering: **71,674**
- Rows absorbed into a multi-source facility: **11,209**
- Flagged for review, not merged: **9,898 pairs**

## Rows per source

- Farm Transparency: 38,069
- EU / EFTA member states: 22,112
- Non-EU (EU-approved): 15,390
- USDA FSIS: 7,241
- China CIFER: 71

## What joined the merges

- `addr`: 1,962 clusters
- `id`: 1,279 clusters
- `geo`: 849 clusters

## Cluster sizes

- 1 source record: 67,709 facilities
- 2 source records: 3,483 facilities
- 3 source records: 350 facilities
- 4 source records: 66 facilities
- 5 source records: 33 facilities
- 6 source records: 4 facilities
- 7 source records: 2 facilities
- 8 source records: 3 facilities
- 9 source records: 1 facilities
- 10 source records: 1 facilities
- 20 source records: 1 facilities
- 22 source records: 1 facilities
- 33 source records: 20 facilities

Nothing was discarded. Every input row is inside exactly one facility above, and `out/facilities.json` carries all of them verbatim.
