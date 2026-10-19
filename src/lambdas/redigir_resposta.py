"""Agente 4 - Redigir resposta.

Recebe:  {"pergunta": "...", "colunas": ["..."], "linhas": [[...], ...], "historico": [...]}
         historico é opcional: lista de {"pergunta", "resposta", "sql"} das últimas
         trocas, da mais antiga para a mais recente. Aqui só entram a pergunta e a
         resposta resumida. Sem histórico, funciona como antes.
Devolve: {"resposta": "..."}
"""
import os

import boto3
from botocore.config import Config

REGION = os.environ.get("AWS_REGION", "us-east-2")
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-haiku-5-5")
LINHAS_NO_PROMPT = int(os.environ.get("LINHAS_NO_PROMPT", "100"))
# A tela já manda no máximo 3 trocas; o corte aqui é só uma garantia.
TROCAS_NO_HISTORICO = 3

bedrock = boto3.client(
    "bedrock-runtime", region_name=REGION, config=Config(retries={"max_attempts": 4, "mode": "adaptive"})
)

SYSTEM = (
    "Você é o assistente de dados da Farmazids. Responda em português, de forma clara e curta, "
    "usando somente os dados fornecidos. Se o resultado estiver vazio, diga que não foram encontrados "
    "dados. Não invente números. O usuário também verá a tabela completa, então não a repita inteira.\n\n"
    "Nomes das farmácias: nos dados elas podem aparecer como códigos. Na resposta, escreva sempre: "
    "Farmaponte, VeraCruz, Farmazids, São Paulo (para sao_paulo) e São João.\n"
    "Se aparecer o preço 9999876, ele não é um preço real: significa produto fora de estoque na São Paulo.\n"
    "Se vier uma conversa anterior e a pergunta atual for continuação dela, responda de forma que faça "
    "sentido na sequência (ex.: 'Na São João, a mesma dipirona custa...'). Use somente os números do "
    "resultado atual, nunca os da conversa anterior.\n"
    "Se a pergunta também pedir informação geral sobre o remédio (ex.: 'para que serve a dipirona e "
    "quanto custa?'), responda primeiro a parte dos dados e depois acrescente no máximo 2 frases gerais "
    "sobre o remédio, sem indicar dose, tratamento ou diagnóstico, terminando com: 'Para orientação "
    "sobre o seu caso, procure um farmacêutico ou médico.'"
)


def tabela_em_texto(colunas, linhas):
    texto = [" | ".join(colunas)]
    for linha in linhas[:LINHAS_NO_PROMPT]:
        texto.append(" | ".join("" if v is None else str(v) for v in linha))
    return "\n".join(texto)


def texto_historico(historico):
    """Conversa anterior para pôr antes da pergunta: só perguntas e respostas
    resumidas, sem SQL. Vazio se não houver histórico."""
    if not historico:
        return ""
    linhas = ["Conversa anterior (da mais antiga para a mais recente):"]
    for numero, troca in enumerate(historico[-TROCAS_NO_HISTORICO:], start=1):
        linhas.append(f"{numero}. Pergunta: {troca.get('pergunta', '')}")
        linhas.append(f"   Resposta: {troca.get('resposta', '')}")
    return "\n".join(linhas) + "\n\n"


def lambda_handler(event, context):
    colunas, linhas = event.get("colunas", []), event.get("linhas", [])
    historico = event.get("historico") or []
    rotulo = "Pergunta atual" if historico else "Pergunta"
    mensagem = (
        f"{texto_historico(historico)}"
        f"{rotulo}: {event['pergunta']}\n\n"
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
