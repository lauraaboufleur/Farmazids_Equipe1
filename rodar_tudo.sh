#!/bin/bash
# Roda as coletas das farmácias na EC2, envia os resultados para o S3
# e desliga a máquina no final (a menos que esteja em modo manutenção).
set -e

REPO=/home/ubuntu/Farmazids_Equipe1
BUCKET=farmazids-t1-equipe1
DATA=$(date +%Y-%m-%d)

desligar() {
    if [ -f /home/ubuntu/MANUTENCAO ]; then
        echo "=== Modo manutenção: a máquina NÃO vai desligar ==="
    else
        echo "=== Desligando a máquina ==="
        sudo shutdown -h now
    fi
}
trap desligar EXIT

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
