#!/bin/bash
# Roda as coletas das farmácias na EC2, envia os resultados para o S3
# e desliga a máquina no final (a menos que esteja em modo manutenção).
# Cada farmácia roda separada: se uma falhar, a outra continua.
# O git pull é feito pelo serviço (infra/coleta.service), antes deste script.

REPO=/home/ubuntu/Farmazids_Equipe1
BUCKET=farmazids-t1-equipe1
DATA=$(date +%Y-%m-%d)
FALHAS=""

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

# ---------------- FarmaPonte ----------------
(
    set -e
    cd "$REPO/FARMAPONTE"
    echo "=== FarmaPonte: dependências ==="
    .venv/bin/pip install -q -r requirements.txt
    mkdir -p data/raw data/processed

    echo "=== FarmaPonte: extração ==="
    .venv/bin/python src/extraction/farmaponte.py

    echo "=== FarmaPonte: tratamento ==="
    .venv/bin/python src/processing/farmaponte.py

    echo "=== FarmaPonte: enviando para o S3 ==="
    aws s3 cp "data/raw/farmaponte_$DATA.jsonl" \
        "s3://$BUCKET/raw/farmaponte/dt=$DATA/farmaponte.jsonl" --region us-east-2
    aws s3 cp "data/processed/farmaponte_$DATA.parquet" \
        "s3://$BUCKET/processed/farmaponte/dt=$DATA/farmaponte.parquet" --region us-east-2
)
if [ $? -ne 0 ]; then
    echo "!!! FarmaPonte FALHOU"
    FALHAS="$FALHAS FarmaPonte"
fi

# ---------------- Veracruz ----------------
(
    set -e
    cd "$REPO/VERACRUZ"
    echo "=== Veracruz: dependências ==="
    .venv/bin/pip install -q -r requirements.txt
    rm -f produtos_drogaria_veracruz.parquet

    echo "=== Veracruz: extração ==="
    .venv/bin/python src/extraction/veracruz.py

    echo "=== Veracruz: enviando para o S3 ==="
    aws s3 cp produtos_drogaria_veracruz.parquet \
        "s3://$BUCKET/raw/veracruz/dt=$DATA/veracruz.parquet" --region us-east-2
)
if [ $? -ne 0 ]; then
    echo "!!! Veracruz FALHOU"
    FALHAS="$FALHAS Veracruz"
fi

echo "=== Fim: $(date) ==="
if [ -n "$FALHAS" ]; then
    echo "!!! Falharam:$FALHAS"
    exit 1
fi
