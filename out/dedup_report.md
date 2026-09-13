# Deduplication report

Generated 2026-09-13

- Input rows: **82,883**
- Facilities after clustering: **71,910**
- Rows absorbed into a multi-source facility: **10,973**
- Flagged for review, not merged: **9,313 pairs**

## Rows per source

- Farm Transparency: 38,069
- EU / EFTA member states: 22,112
- Non-EU (EU-approved): 15,390
- USDA FSIS: 7,241
- China CIFER: 71

## What joined the merges

- `addr`: 1,964 clusters
- `id`: 1,280 clusters
- `geo`: 673 clusters

## Cluster sizes

- 1 source record: 68,085 facilities
- 2 source records: 3,391 facilities
- 3 source records: 317 facilities
- 4 source records: 59 facilities
- 5 source records: 30 facilities
- 6 source records: 3 facilities
- 8 source records: 2 facilities
- 9 source records: 1 facilities
- 10 source records: 1 facilities
- 20 source records: 1 facilities
- 33 source records: 20 facilities

Nothing was discarded. Every input row is inside exactly one facility above, and `out/facilities.json` carries all of them verbatim.
