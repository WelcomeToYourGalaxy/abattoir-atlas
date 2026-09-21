# Deduplication report

Generated 2026-09-21

- Input rows: **205,042**
- Facilities after clustering: **184,849**
- Rows absorbed into a multi-source facility: **20,193**
- Flagged for review, not merged: **49,679 pairs**

## Rows per source

- Farm Transparency: 75,282
- EU industrial permits: 32,030
- EU animal health: 29,340
- EU / EFTA member states: 22,112
- Non-EU (EU-approved): 15,390
- Trase: 15,119
- USDA FSIS: 7,241
- Brazil SIF: 3,147
- OpenStreetMap: 2,717
- NZ MPI: 1,641
- Canada CFIA: 952
- China CIFER: 71

## What joined the merges

- `id`: 4,356 clusters
- `geo`: 3,632 clusters
- `addr`: 3,565 clusters

## Cluster sizes

- 1 source record: 173,751 facilities
- 2 source records: 9,794 facilities
- 3 source records: 874 facilities
- 4 source records: 234 facilities
- 5 source records: 93 facilities
- 6 source records: 30 facilities
- 7 source records: 15 facilities
- 8 source records: 13 facilities
- 9 source records: 6 facilities
- 10 source records: 4 facilities
- 11 source records: 1 facilities
- 12 source records: 2 facilities
- 13 source records: 1 facilities
- 14 source records: 1 facilities
- 17 source records: 1 facilities
- 18 source records: 3 facilities
- 19 source records: 1 facilities
- 20 source records: 1 facilities
- 21 source records: 1 facilities
- 22 source records: 1 facilities
- 29 source records: 1 facilities
- 33 source records: 20 facilities
- 37 source records: 1 facilities

Nothing was discarded. Every input row is inside exactly one facility above, and `out/facilities.json` carries all of them verbatim.
