"""Agente 1 - Gerar SQL.

Recebe:  {"pergunta": "...", "erro_anterior": ""}   (erro_anterior vazio na 1ª vez)
Devolve: {"sql": "..."}
Se a pergunta não puder ser respondida com as tabelas, devolve {"sql": "CANNOT_ANSWER"}.
"""
import os
import re

import boto3
from botocore.config import Config

REGION = os.environ.get("AWS_REGION", "us-east-2")
GLUE_DATABASE = os.environ.get("GLUE_DATABASE", "t1_eq1_farmazids")
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-haiku-5-5")
# Mesmas tabelas que o agente Validar SQL aceita; o modelo só enxerga essas.
ALLOWED_TABLES = {
    t.strip().lower()
    for t in os.environ.get("ALLOWED_TABLES", "vw_medicamentos,vw_precos_concorrentes,vw_vendas").split(",")
    if t.strip()
}

_retry = Config(retries={"max_attempts": 4, "mode": "adaptive"})
glue = boto3.client("glue", region_name=REGION, config=_retry)
bedrock = boto3.client("bedrock-runtime", region_name=REGION, config=_retry)

# Conhecimento sobre os dados que o Glue não tem.
DICAS_DADOS = (
    "- vw_precos_concorrentes: preços coletados nos sites das farmácias concorrentes "
    "(preco_sem_desconto, preco_cartao, preco_pix, em_estoque), um registro por produto e coleta. "
    "data_coleta é um timestamp (ex.: 2026-10-11 15:47:43); para 'preço atual', use a coleta mais recente "
    "de cada farmácia e produto. categorias é um array: use contains(categorias, '...') ou UNNEST.\n"
    "- vw_vendas: vendas da Farmazids (uma linha por venda, com quantidade_vendida e data_venda).\n"
    "- vw_medicamentos: catálogo da Farmazids com o preço próprio (preco_farmazids).\n"
    "- As três views se ligam pela coluna ean (código de barras do produto).\n"
    "- A coluna farmacia pode vir escrita de formas diferentes em cada view. Filtre sempre com LOWER e "
    "aceitando as variações: Farmaponte -> LOWER(farmacia) = 'farmaponte'; VeraCruz -> LOWER(farmacia) = "
    "'veracruz'; Farmazids -> LOWER(farmacia) = 'farmazids'; São Paulo (Drogaria São Paulo) -> "
    "LOWER(farmacia) IN ('sao_paulo', 'são paulo', 'sao paulo'); São João -> LOWER(farmacia) IN "
    "('são joão', 'sao_joao', 'sao joao').\n"
    "- Quando o produto está fora de estoque, a São Paulo grava preco_sem_desconto = 9999876. Esse valor "
    "não é um preço: exclua-o de médias, mínimos, máximos e comparações (preco_sem_desconto <> 9999876)."
)

_schema_cache = None


def carregar_schema():
    """Lê do Glue Data Catalog as views permitidas com suas colunas (guardado entre chamadas)."""
    global _schema_cache
    if _schema_cache is None:
        linhas = []
        for page in glue.get_paginator("get_tables").paginate(DatabaseName=GLUE_DATABASE):
            for t in page["TableList"]:
                if t["Name"].lower() not in ALLOWED_TABLES:
                    continue
                colunas = t.get("StorageDescriptor", {}).get("Columns", []) + t.get("PartitionKeys", [])
                texto = f"{t['Name']}(" + ", ".join(f"{c['Name']} {c['Type']}" for c in colunas) + ")"
                if t.get("PartitionKeys"):
                    texto += " -- partições: " + ", ".join(c["Name"] for c in t["PartitionKeys"])
                linhas.append(texto)
        _schema_cache = "\n".join(linhas)
    return _schema_cache


def system_prompt():
    return (
        "Você converte perguntas em português em SQL para o Amazon Athena (dialeto Trino). "
        f"Use somente as views abaixo, do banco {GLUE_DATABASE}.\n\n"
        f"Views:\n{carregar_schema()}\n\n"
        "Regras:\n"
        "- Gere uma única consulta SELECT (ou WITH ... SELECT), somente leitura.\n"
        "- Sempre termine com LIMIT (no máximo 200).\n"
        "- Quando a tabela tiver partições, filtre por elas sempre que a pergunta permitir.\n"
        "- Responda APENAS com o SQL, sem explicações e sem blocos de código.\n"
        "- Se a pergunta não puder ser respondida com essas views, responda exatamente CANNOT_ANSWER.\n\n"
        f"Sobre os dados:\n{DICAS_DADOS}"
    )


def limpar_sql(texto):
    texto = re.sub(r"```(sql)?", "", texto, flags=re.IGNORECASE)
    return texto.strip().rstrip(";").strip()


def lambda_handler(event, context):
    pergunta = event["pergunta"]
    erro_anterior = event.get("erro_anterior") or ""

    mensagem = pergunta
    if erro_anterior:
        mensagem += f"\n\nA tentativa anterior foi reprovada:\n{erro_anterior}\n\nCorrija o SQL."

    resp = bedrock.converse(
        modelId=MODEL_ID,
        system=[{"text": system_prompt()}],
        messages=[{"role": "user", "content": [{"text": mensagem}]}],
        inferenceConfig={"maxTokens": 4000},
    )
    texto = next(b["text"] for b in resp["output"]["message"]["content"] if "text" in b)
    sql = limpar_sql(texto)
    if "CANNOT_ANSWER" in sql:
        sql = "CANNOT_ANSWER"
    return {"sql": sql}
