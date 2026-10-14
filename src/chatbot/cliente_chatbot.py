"""Faz a ponte entre a tela (Streamlit) e o Step Functions do chatbot.

Dois modos:
- simulado (padrão): devolve uma resposta de teste, sem chamar a AWS.
  Serve para montar e testar a tela antes de ligar no backend.
- real: chama a máquina Expressa do Step Functions e espera a resposta.

Configuração por variáveis de ambiente (nada de credencial no código):
- CHATBOT_MODO_SIMULADO = "1" (simulado) ou "0" (real)
- CHATBOT_STATE_MACHINE_ARN = ARN da máquina Expressa
"""

import json
import os
import time

REGIAO = "us-east-2"
ARN_MAQUINA = os.environ.get("CHATBOT_STATE_MACHINE_ARN", "")
MODO_SIMULADO = os.environ.get("CHATBOT_MODO_SIMULADO", "1") == "1"
TAMANHO_MAXIMO_PERGUNTA = 500

# Os nomes vêm dos estados Fail da máquina (Error de cada um).
MENSAGENS_ERRO = {
    "SQLInvalido": "Não consegui montar uma consulta segura para essa pergunta. "
                   "Tente reformular, citando o medicamento ou a farmácia.",
    "ConsultaRecusada": "A consulta não pôde ser executada nos dados. "
                        "Tente reformular a pergunta.",
    "ErroServico": "Um dos serviços da AWS não respondeu. Tente de novo em instantes.",
}
MENSAGEM_GENERICA = "Algo deu errado ao responder. Tente de novo em instantes."


def _falha(mensagem, detalhe=None):
    # O detalhe técnico vai só para o terminal, nunca para a tela do usuário.
    if detalhe:
        print(f"[chatbot] erro: {detalhe}")
    return {"ok": False, "resposta": mensagem, "sql": None, "colunas": [], "linhas": []}


def _perguntar_simulado(pergunta):
    time.sleep(1.5)  # imita o tempo de resposta do backend
    return {
        "ok": True,
        "resposta": f"(modo simulado) Você perguntou: \"{pergunta}\". "
                    "Quando o backend estiver ligado, a resposta de verdade aparece aqui.",
        "sql": "SELECT farmacia, count(*) AS produtos\n"
               "FROM vw_precos_concorrentes\n"
               "WHERE dt = '2026-10-12'\n"
               "GROUP BY farmacia\n"
               "LIMIT 10",
        "colunas": ["farmacia", "produtos"],
        "linhas": [["farmaponte", "5419"], ["sao_joao", "6881"],
                   ["veracruz", "4480"], ["sao_paulo", "11629"]],
    }


def _perguntar_aws(pergunta):
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    if not ARN_MAQUINA:
        return _falha(MENSAGEM_GENERICA, "CHATBOT_STATE_MACHINE_ARN não foi definido.")

    cliente = boto3.client("stepfunctions", region_name=REGIAO)
    try:
        execucao = cliente.start_sync_execution(
            stateMachineArn=ARN_MAQUINA,
            input=json.dumps({"pergunta": pergunta}, ensure_ascii=False),
        )
    except (BotoCoreError, ClientError) as erro:
        return _falha(MENSAGENS_ERRO["ErroServico"], str(erro))

    if execucao["status"] != "SUCCEEDED":
        codigo = execucao.get("error")
        mensagem = MENSAGENS_ERRO.get(codigo, MENSAGEM_GENERICA)
        return _falha(mensagem, f"{execucao['status']} {codigo}: {execucao.get('cause')}")

    saida = json.loads(execucao["output"])
    return {
        "ok": True,
        "resposta": saida.get("resposta", ""),
        "sql": saida.get("sql"),
        "colunas": saida.get("colunas") or [],
        "linhas": saida.get("linhas") or [],
    }


def perguntar(pergunta):
    """Recebe a pergunta e devolve sempre as mesmas chaves:
    ok, resposta, sql, colunas, linhas."""
    pergunta = (pergunta or "").strip()
    if not pergunta:
        return _falha("Escreva uma pergunta.")
    if len(pergunta) > TAMANHO_MAXIMO_PERGUNTA:
        return _falha(f"A pergunta pode ter no máximo {TAMANHO_MAXIMO_PERGUNTA} caracteres.")

    if MODO_SIMULADO:
        return _perguntar_simulado(pergunta)
    return _perguntar_aws(pergunta)
