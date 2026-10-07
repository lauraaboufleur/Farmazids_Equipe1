#!/bin/bash
# Roda as coletas das farmácias na EC2.

REPO=/home/ubuntu/Farmazids_Equipe1
DATA=$(date +%Y-%m-%d)

echo "=== Início: $(date) ==="

cd "$REPO"
git pull

echo "=== FarmaPonte: extração ==="
cd "$REPO/FARMAPONTE"
mkdir -p data/raw data/processed
.venv/bin/python src/extraction/farmaponte.py

echo "=== FarmaPonte: tratamento ==="
.venv/bin/python src/processing/farmaponte.py

echo "=== Veracruz: extração ==="
cd "$REPO/VERACRUZ"
.venv/bin/python src/extraction/veracruz.py

echo "=== Fim: $(date) ==="

