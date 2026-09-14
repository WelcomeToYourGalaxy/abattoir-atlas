# Deduplication report

Generated 2026-09-14

- Input rows: **184,059**
- Facilities after clustering: **168,804**
- Rows absorbed into a multi-source facility: **15,255**
- Flagged for review, not merged: **43,114 pairs**

## Rows per source

- Farm Transparency: 75,282
- EU industrial permits: 32,030
- EU animal health: 29,340
- EU / EFTA member states: 22,112
- Non-EU (EU-approved): 15,390
- USDA FSIS: 7,241
- NZ MPI: 1,641
- Canada CFIA: 952
- China CIFER: 71

## What joined the merges

- `addr`: 3,462 clusters
- `geo`: 2,475 clusters
- `id`: 1,364 clusters

## Cluster sizes

- 1 source record: 161,800 facilities
- 2 source records: 6,081 facilities
- 3 source records: 654 facilities
- 4 source records: 152 facilities
- 5 source records: 65 facilities
- 6 source records: 12 facilities
- 7 source records: 6 facilities
- 8 source records: 5 facilities
- 9 source records: 3 facilities
- 10 source records: 1 facilities
- 13 source records: 1 facilities
- 19 source records: 1 facilities
- 20 source records: 1 facilities
- 21 source records: 1 facilities
- 22 source records: 1 facilities
- 33 source records: 20 facilities

Nothing was discarded. Every input row is inside exactly one facility above, and `out/facilities.json` carries all of them verbatim.
