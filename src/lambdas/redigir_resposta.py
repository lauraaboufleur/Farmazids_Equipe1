"""Agente 4 - Redigir resposta.

Recebe:  {"pergunta": "...", "colunas": ["..."], "linhas": [[...], ...]}
Devolve: {"resposta": "..."}
"""
import os

import boto3
from botocore.config import Config

REGION = os.environ.get("AWS_REGION", "us-east-2")
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-haiku-5-5")
LINHAS_NO_PROMPT = int(os.environ.get("LINHAS_NO_PROMPT", "100"))

bedrock = boto3.client(
    "bedrock-runtime", region_name=REGION, config=Config(retries={"max_attempts": 4, "mode": "adaptive"})
)

SYSTEM = (
    "Você é o assistente de dados da Farmazids. Responda em português, de forma clara e curta, "
    "usando somente os dados fornecidos. Se o resultado estiver vazio, diga que não foram encontrados "
    "dados. Não invente números. O usuário também verá a tabela completa, então não a repita inteira.\n\n"
    "Nomes das farmácias: nos dados elas podem aparecer como códigos. Na resposta, escreva sempre: "
    "Farmaponte, VeraCruz, Farmazids, São Paulo (para sao_paulo) e São João.\n"
    "Se aparecer o preço 9999876, ele não é um preço real: significa produto fora de estoque na São Paulo."
)


def tabela_em_texto(colunas, linhas):
    texto = [" | ".join(colunas)]
    for linha in linhas[:LINHAS_NO_PROMPT]:
        texto.append(" | ".join("" if v is None else str(v) for v in linha))
    return "\n".join(texto)


def lambda_handler(event, context):
    colunas, linhas = event.get("colunas", []), event.get("linhas", [])
    mensagem = (
        f"Pergunta: {event['pergunta']}\n\n"
        f"Resultado ({len(linhas)} linhas, colunas separadas por |):\n"
        f"{tabela_em_texto(colunas, linhas)}"
    )
    resp = bedrock.converse(
        modelId=MODEL_ID,
        system=[{"text": SYSTEM}],
        messages=[{"role": "user", "content": [{"text": mensagem}]}],
        inferenceConfig={"maxTokens": 4000},
    )
    texto = next(b["text"] for b in resp["output"]["message"]["content"] if "text" in b)
    return {"resposta": texto.strip()}
