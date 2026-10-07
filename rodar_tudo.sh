#!/bin/bash
# Roda as coletas das farmácias na EC2 e envia os resultados para o S3.
set -e

REPO=/home/ubuntu/Farmazids_Equipe1
BUCKET=farmazids-t1-equipe1
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

echo "=== Enviando para o S3 ==="
aws s3 cp "$REPO/FARMAPONTE/data/raw/farmaponte_$DATA.jsonl" \
    "s3://$BUCKET/raw/farmaponte/dt=$DATA/farmaponte.jsonl" --region us-east-2
aws s3 cp "$REPO/FARMAPONTE/data/processed/farmaponte_$DATA.parquet" \
    "s3://$BUCKET/processed/farmaponte/dt=$DATA/farmaponte.parquet" --region us-east-2
aws s3 cp "$REPO/VERACRUZ/produtos_drogaria_veracruz.parquet" \
    "s3://$BUCKET/raw/veracruz/dt=$DATA/veracruz.parquet" --region us-east-2

echo "=== Fim: $(date) ==="
