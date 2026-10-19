"""Faz a ponte entre a tela (Streamlit) e o Step Functions do chatbot.

Dois modos:
- simulado (padrão): devolve uma resposta de teste, sem chamar a AWS.
  Serve para montar e testar a tela antes de ligar no backend.
- real: chama a máquina Expressa do Step Functions e espera a resposta.

Configuração por variáveis de ambiente (nada de credencial no código):
- CHATBOT_MODO_SIMULADO = "1" (simulado) ou "0" (real)
- CHATBOT_STATE_MACHINE_ARN = ARN da máquina Expressa
- CHATBOT_CACHE_MINUTOS = por quantos minutos guardar uma resposta (padrão 30; 0 desliga)

Dois cuidados com o tempo de resposta:
- o cliente do boto3 é criado uma vez só e reaproveitado. Criar um novo a cada
  pergunta obriga a abrir outra conexão com a AWS, o que custa alguns décimos
  de segundo toda vez;
- respostas certas ficam guardadas por alguns minutos. Se alguém repetir a
  mesma pergunta, ela volta na hora, sem chamar Bedrock e Athena de novo.
  Como a coleta só roda a cada dois dias, isso não deixa os dados velhos.

Memória da conversa: junto com a pergunta vai o histórico das últimas trocas
(pergunta, resposta resumida e SQL usado), montado pela tela. O SQL serve só
para o backend entender perguntas como "e na São João?"; nunca vai para a tela.
"""

import json
import os
import threading
import time
from functools import lru_cache

REGIAO = "us-east-2"
ARN_MAQUINA = os.environ.get("CHATBOT_STATE_MACHINE_ARN", "")
MODO_SIMULADO = os.environ.get("CHATBOT_MODO_SIMULADO", "1") == "1"
TAMANHO_MAXIMO_PERGUNTA = 500
CACHE_SEGUNDOS = int(os.environ.get("CHATBOT_CACHE_MINUTOS", "30")) * 60
CACHE_MAXIMO = 200  # quantas perguntas guardar, no máximo

# Os nomes vêm dos estados Fail da máquina (Error de cada um).
MENSAGENS_ERRO = {
    "SQLInvalido": "Não consegui montar uma consulta segura para essa pergunta. "
                   "Tente reformular, citando o medicamento ou a farmácia.",
    "ConsultaRecusada": "A consulta não pôde ser executada nos dados. "
                        "Tente reformular a pergunta.",
    "ErroServico": "Um dos serviços da AWS não respondeu. Tente de novo em instantes.",
}
MENSAGEM_GENERICA = "Algo deu errado ao responder. Tente de novo em instantes."

# Respostas guardadas: {chave da pergunta: (momento em que foi guardada, resultado)}.
# Fica na memória do processo, então vale para todas as pessoas usando o app.
_cache = {}
_trava_cache = threading.Lock()


def _falha(mensagem, detalhe=None):
    # O detalhe técnico vai só para o terminal (ou para os logs do Streamlit Cloud),
    # nunca para a tela do usuário.
    if detalhe:
        print(f"[chatbot] erro: {detalhe}")
    return {"ok": False, "resposta": mensagem, "sql": None, "colunas": [], "linhas": [], "tipo": None}


def _normalizar(pergunta):
    """"Qual o preço  da Dipirona?" e "qual o preço da dipirona" viram a mesma chave."""
    return " ".join(pergunta.lower().split()).rstrip("?!. ")


def _chave_cache(pergunta, historico):
    """A mesma pergunta pode ter respostas diferentes conforme a conversa:
    "e na São João?" depois de dipirona não é o mesmo que depois de amoxicilina.
    Por isso a chave leva também as perguntas e os SQLs anteriores. Sem
    histórico, a chave é só a pergunta, como antes."""
    partes = [_normalizar(troca["pergunta"]) + "\n" + " ".join((troca.get("sql") or "").lower().split())
              for troca in historico]
    return "\n--\n".join(partes + [_normalizar(pergunta)])


def _buscar_no_cache(chave):
    if CACHE_SEGUNDOS <= 0:
        return None
    with _trava_cache:
        guardado = _cache.get(chave)
        if guardado is None:
            return None
        momento, resultado = guardado
        if time.time() - momento > CACHE_SEGUNDOS:
            del _cache[chave]
            return None
        return dict(resultado)


def _guardar_no_cache(chave, resultado):
    # Só guarda resposta certa: erro não pode ficar "preso" por 30 minutos.
    if CACHE_SEGUNDOS <= 0 or not resultado["ok"]:
        return
    with _trava_cache:
        if len(_cache) >= CACHE_MAXIMO:
            mais_antiga = min(_cache, key=lambda k: _cache[k][0])
            del _cache[mais_antiga]
        _cache[chave] = (time.time(), dict(resultado))


@lru_cache(maxsize=1)
def _cliente_step_functions():
    """Criado na primeira pergunta e reaproveitado nas seguintes."""
    import boto3
    from botocore.config import Config

    config = Config(
        connect_timeout=5,
        read_timeout=120,  # a máquina Expressa responde de forma síncrona, pode levar alguns segundos
        retries={"max_attempts": 2, "mode": "standard"},
    )
    return boto3.client("stepfunctions", region_name=REGIAO, config=config)


def _perguntar_simulado(pergunta, historico):
    time.sleep(1.5)  # imita o tempo de resposta do backend
    contexto = f" (com {len(historico)} troca(s) anterior(es) no histórico)" if historico else ""
    return {
        "ok": True,
        "resposta": f"(modo simulado) Você perguntou: \"{pergunta}\"{contexto}. "
                    "Quando o backend estiver ligado, a resposta de verdade aparece aqui.",
        "sql": "SELECT farmacia, nome, preco_pix, preco_cartao, em_estoque, data_coleta\n"
               "FROM vw_precos_concorrentes\n"
               "WHERE lower(nome) LIKE '%dipirona%'\n"
               "LIMIT 10",
        "colunas": ["farmacia", "nome", "preco_pix", "preco_cartao", "em_estoque", "data_coleta"],
        "linhas": [
            ["sao_joao", "Dipirona Monoidratada 500mg 10 Comprimidos", "4.19", "4.49", "true", "2026-10-16"],
            ["farmaponte", "Dipirona Monoidratada 500mg 10 Comprimidos", "4.29", "4.79", "true", "2026-10-16"],
            ["veracruz", "Dipirona Gotas 500mg/ml 20ml", "7.90", "8.49", "false", "2026-10-16"],
            ["sao_paulo", "Novalgina 1g 10 Comprimidos", "21.99", "23.90", "true", "2026-10-16"],
        ],
        "tipo": "dados",
    }


def _perguntar_aws(pergunta, historico):
    from botocore.exceptions import BotoCoreError, ClientError

    if not ARN_MAQUINA:
        return _falha(MENSAGEM_GENERICA, "CHATBOT_STATE_MACHINE_ARN não foi definido.")

    try:
        execucao = _cliente_step_functions().start_sync_execution(
            stateMachineArn=ARN_MAQUINA,
            input=json.dumps({"pergunta": pergunta, "historico": historico}, ensure_ascii=False),
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
        # "dados", "teorica" ou "sem_dados". A máquina antiga não manda: fica None.
        "tipo": saida.get("tipo"),
    }


def perguntar(pergunta, historico=None):
    """Recebe a pergunta e o histórico (lista de {"pergunta", "resposta", "sql"},
    da troca mais antiga para a mais recente; vazio na primeira pergunta) e
    devolve sempre as mesmas chaves:
    ok, resposta, sql, colunas, linhas, tipo, duracao (segundos) e do_cache."""
    inicio = time.perf_counter()
    pergunta = (pergunta or "").strip()
    historico = historico or []
    if not pergunta:
        resultado = _falha("Escreva uma pergunta.")
    elif len(pergunta) > TAMANHO_MAXIMO_PERGUNTA:
        resultado = _falha(f"A pergunta pode ter no máximo {TAMANHO_MAXIMO_PERGUNTA} caracteres.")
    else:
        chave = _chave_cache(pergunta, historico)
        guardado = _buscar_no_cache(chave)
        if guardado is not None:
            guardado["do_cache"] = True
            guardado["duracao"] = time.perf_counter() - inicio
            return guardado
        if MODO_SIMULADO:
            resultado = _perguntar_simulado(pergunta, historico)
        else:
            resultado = _perguntar_aws(pergunta, historico)
        _guardar_no_cache(chave, resultado)

    resultado["do_cache"] = False
    resultado["duracao"] = time.perf_counter() - inicio
    # Fica no log (terminal ou "Manage app" no Streamlit Cloud): ajuda a medir o tempo real.
    # O SQL não entra no log.
    print(f"[chatbot] {resultado['duracao']:.1f} s | ok={resultado['ok']} | "
          f"historico={len(historico)} | {pergunta[:60]}")
    return resultado
