"""Agente 5 - Responder pergunta teórica.

Responde perguntas gerais sobre remédios e saúde ("para que serve a dipirona?",
"qual a diferença entre genérico e similar?") direto com o Bedrock, sem SQL e
sem Athena. A máquina só chama este agente quando o Gerar SQL devolve
PERGUNTA_TEORICA.

Recebe:  {"pergunta": "...", "historico": [...]}
         historico é opcional: lista de {"pergunta", "resposta", "sql"} das últimas
         trocas, da mais antiga para a mais recente. Aqui só entram a pergunta e a
         resposta resumida. Sem histórico, funciona do mesmo jeito.
Devolve: {"resposta": "..."}
"""
import os
import time

import boto3
from botocore.config import Config

REGION = os.environ.get("AWS_REGION", "us-east-2")
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-haiku-5-5")
# Resposta curta: o tempo de resposta cresce com o tamanho do texto gerado.
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "600"))
# A tela já manda no máximo 3 trocas; o corte aqui é só uma garantia.
TROCAS_NO_HISTORICO = 3

# O aviso vai pelo código, e não pelo modelo: assim nunca falta e nunca sai repetido.
AVISO = ("Estas são informações gerais e não substituem a orientação de um profissional. "
         "Em caso de dúvida, procure um farmacêutico ou médico.")

bedrock = boto3.client(
    "bedrock-runtime", region_name=REGION, config=Config(retries={"max_attempts": 4, "mode": "adaptive"})
)

SYSTEM = (
    "Você é o assistente da Farmazids e responde perguntas gerais sobre remédios e saúde, em português "
    "simples, de forma clara e curta: no máximo 5 frases, sem títulos e sem listas longas.\n\n"
    "Regras:\n"
    "- Dê apenas informação geral e educativa: para que o remédio costuma ser usado, como age, a que "
    "classe pertence, diferenças entre remédios ou entre genérico, similar e referência.\n"
    "- Nunca indique dose, posologia, tratamento ou diagnóstico para uma pessoa. Se perguntarem quanto "
    "tomar, se podem tomar, se podem misturar remédios ou o que tomar para um sintoma, explique que isso "
    "depende de cada caso e precisa ser avaliado por um farmacêutico ou médico.\n"
    "- Em sinais de emergência (intoxicação, alguém que tomou remédio demais, reação grave), oriente a "
    "ligar já para o SAMU (192) ou para o Disque-Intoxicação da Anvisa (0800 722 6001).\n"
    "- Se a pergunta não for sobre remédios ou saúde, diga com educação que você responde perguntas "
    "sobre remédios e sobre os dados da Farmazids (preços, concorrentes, vendas e estoque).\n"
    "- Se vier uma conversa anterior e a pergunta atual for continuação dela (ex.: 'e para que ela "
    "serve?'), entenda a qual remédio ela se refere.\n"
    "- Não escreva aviso para procurar um profissional no final: ele é acrescentado automaticamente."
)


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
    # A tela pode mandar {"modo": "rapida"} no fim do histórico (para a redigir-resposta): não é uma troca.
    historico = [t for t in (event.get("historico") or []) if not (isinstance(t, dict) and "modo" in t)]
    rotulo = "Pergunta atual" if historico else "Pergunta"
    mensagem = f"{texto_historico(historico)}{rotulo}: {event['pergunta']}"
    inicio = time.perf_counter()
    resp = bedrock.converse(
        modelId=MODEL_ID,
        system=[{"text": SYSTEM}],
        messages=[{"role": "user", "content": [{"text": mensagem}]}],
        inferenceConfig={"maxTokens": MAX_TOKENS},
    )
    print(f"[tempo] etapa=bedrock {time.perf_counter() - inicio:.2f} s")
    uso = resp.get("usage", {})
    print(f"[tokens] entrada={uso.get('inputTokens')} saida={uso.get('outputTokens')}")
    texto = next(b["text"] for b in resp["output"]["message"]["content"] if "text" in b)
    return {"resposta": f"{texto.strip()}\n\n{AVISO}"}
