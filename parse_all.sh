#!/usr/bin/env bash
# Parses every source file present in raw/. Safe to run repeatedly: each source
# replaces its own previous records rather than appending, so re-running after a
# fresh download updates that source and leaves the others alone.
set -euo pipefail
snap="${1:-$(date -u +%Y-%m-%d)}"

if [ -f raw/fsis_mpi_directory.csv ]; then
  echo "== FSIS"
  python run.py parse --source us_fsis_mpi \
    --file fsis_mpi_directory.csv \
    --demographic fsis_demographic.csv --snapshot "$snap"
fi

for dir in eu_traces_third_country eu_member_states uk_fsa; do
  if compgen -G "raw/$dir/*.csv" > /dev/null 2>&1; then
    echo "== $dir"
    python run.py parse --source "$dir" --snapshot "$snap"
  fi
done

if compgen -G "raw/eu_traces_animal_health/*.csv" > /dev/null 2>&1; then
  echo "== EU TRACES animal health"
  python run.py parse --source eu_traces_animal_health --snapshot "$snap"
fi

if [ -f raw/eu_ied.csv ]; then
  echo "== EU IED installations"
  python run.py parse --source eu_industrial_emissions --file eu_ied.csv --snapshot "$snap"
fi

if [ -f raw/ca_cfia.csv ]; then
  echo "== Canada CFIA"
  python run.py parse --source ca_cfia --file ca_cfia.csv --snapshot "$snap"
fi

if [ -f raw/nz_mpi.csv ]; then
  echo "== New Zealand MPI"
  python run.py parse --source nz_mpi --file nz_mpi.csv --snapshot "$snap"
fi

if [ -f raw/br_sif.csv ]; then
  echo "== Brazil SIF"
  python run.py parse --source br_sif --file br_sif.csv --snapshot "$snap"
fi

if [ -f raw/br_trase.geo.json ]; then
  echo "== Brazil, Trase"
  python run.py parse --source br_trase --file br_trase.geo.json --snapshot "$snap"
fi

if [ -f raw/cifer.jsonl ]; then
  echo "== CIFER"
  python run.py parse --source cifer_china --file cifer.jsonl --snapshot "$snap"
fi

if compgen -G "raw/farm_transparency/*" > /dev/null 2>&1; then
  echo "== Farm Transparency Project"
  python run.py parse --source farm_transparency --snapshot "$snap"
fi

if [ -f raw/osm.json ]; then
  echo "== OpenStreetMap"
  python run.py parse --source osm_overpass --file osm.json --snapshot "$snap"
fi
