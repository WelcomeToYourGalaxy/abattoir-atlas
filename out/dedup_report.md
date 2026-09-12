# Deduplication report

Generated 2026-09-12

- Input rows: **82,883**
- Facilities after clustering: **71,996**
- Rows absorbed into a multi-source facility: **10,887**
- Flagged for review, not merged: **9,286 pairs**

## Rows per source

- Farm Transparency: 38,069
- EU / EFTA member states: 22,112
- Non-EU (EU-approved): 15,390
- USDA FSIS: 7,241
- China CIFER: 71

## What joined the merges

- `addr`: 1,905 clusters
- `id`: 1,280 clusters
- `geo`: 673 clusters

## Cluster sizes

- 1 source record: 68,228 facilities
- 2 source records: 3,357 facilities
- 3 source records: 298 facilities
- 4 source records: 57 facilities
- 5 source records: 28 facilities
- 6 source records: 3 facilities
- 8 source records: 2 facilities
- 9 source records: 1 facilities
- 10 source records: 1 facilities
- 20 source records: 1 facilities
- 33 source records: 20 facilities

Nothing was discarded. Every input row is inside exactly one facility above, and `out/facilities.json` carries all of them verbatim.
