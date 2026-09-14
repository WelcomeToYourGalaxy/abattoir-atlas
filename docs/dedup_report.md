# Deduplication report

Generated 2026-09-14

- Input rows: **184,059**
- Facilities after clustering: **168,572**
- Rows absorbed into a multi-source facility: **15,487**
- Flagged for review, not merged: **42,934 pairs**

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
- `geo`: 2,662 clusters
- `id`: 1,364 clusters

## Cluster sizes

- 1 source record: 161,399 facilities
- 2 source records: 6,209 facilities
- 3 source records: 682 facilities
- 4 source records: 159 facilities
- 5 source records: 69 facilities
- 6 source records: 13 facilities
- 7 source records: 7 facilities
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
