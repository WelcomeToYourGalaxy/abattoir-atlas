# Deduplication report

Generated 2026-09-25

- Input rows: **205,119**
- Facilities after clustering: **186,588**
- Rows absorbed into a multi-source facility: **18,531**
- Flagged for review, not merged: **49,634 pairs**

## Rows per source

- Farm Transparency: 75,282
- EU industrial permits: 32,030
- EU animal health: 29,340
- EU / EFTA member states: 22,112
- Non-EU (EU-approved): 15,390
- Trase: 15,119
- USDA FSIS: 7,241
- Brazil SIF: 3,147
- OpenStreetMap: 2,794
- NZ MPI: 1,641
- Canada CFIA: 952
- China CIFER: 71

## What joined the merges

- `addr`: 3,578 clusters
- `geo`: 3,506 clusters
- `id`: 3,157 clusters

## Cluster sizes

- 1 source record: 176,631 facilities
- 2 source records: 8,919 facilities
- 3 source records: 703 facilities
- 4 source records: 181 facilities
- 5 source records: 76 facilities
- 6 source records: 19 facilities
- 7 source records: 13 facilities
- 8 source records: 9 facilities
- 9 source records: 5 facilities
- 10 source records: 2 facilities
- 11 source records: 1 facilities
- 12 source records: 1 facilities
- 13 source records: 1 facilities
- 14 source records: 1 facilities
- 18 source records: 1 facilities
- 19 source records: 1 facilities
- 20 source records: 1 facilities
- 21 source records: 1 facilities
- 22 source records: 1 facilities
- 29 source records: 1 facilities
- 33 source records: 20 facilities

Nothing was discarded. Every input row is inside exactly one facility above, and `out/facilities.json` carries all of them verbatim.
