"""Faz a ponte entre a tela (Streamlit) e o Step Functions do chatbot.

Dois modos:
- simulado (padrão): devolve uma resposta de teste, sem chamar a AWS.
  Serve para montar e testar a tela antes de ligar no backend.
- real: chama a máquina Expressa do Step Functions e espera a resposta.

Configuração por variáveis de ambiente (nada de credencial no código):
- CHATBOT_MODO_SIMULADO = "1" (simulado) ou "0" (real)
- CHATBOT_STATE_MACHINE_ARN = ARN da máquina Expressa
- CHATBOT_CACHE_HORAS = por quantas horas, no máximo, guardar uma resposta (padrão 24; 0 desliga).
  Antes disso, a resposta sai do cache assim que chega uma coleta nova (data lida do S3).
- CHATBOT_BUCKET = bucket da coleta (padrão farmazids-t1-equipe1), para saber a data da última
- CHATBOT_POLLY_SIMULADO = no modo simulado, "ok" (padrão: um áudio de teste) ou "falha"
  (finge que o Polly falhou, para testar a volta para a voz do navegador)

Dois cuidados com o tempo de resposta:
- o cliente do boto3 é criado uma vez só e reaproveitado. Criar um novo a cada
  pergunta obriga a abrir outra conexão com a AWS, o que custa alguns décimos
  de segundo toda vez;
- respostas certas ficam guardadas até a próxima coleta (ou 24 h). Se alguém
  repetir a mesma pergunta, ela volta na hora, sem chamar Bedrock e Athena de novo.
  Os dados só mudam quando chega uma coleta nova, então isso não deixa os dados velhos.

Modo rápido: a máquina só repassa pergunta, historico, colunas e linhas. Então o modo vai
como último item do histórico ({"modo": "rapida"}); a redigir-resposta o lê e monta a
frase sem Bedrock (as outras Lambdas ignoram esse item).

Memória da conversa: junto com a pergunta vai o histórico das últimas trocas
(pergunta, resposta resumida e SQL usado), montado pela tela. O SQL serve só
para o backend entender perguntas como "e na São João?"; nunca vai para a tela.
"""

import io
import json
import os
import re
import threading
import time
import unicodedata
import wave
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

REGIAO = "us-east-2"
ARN_MAQUINA = os.environ.get("CHATBOT_STATE_MACHINE_ARN", "")
MODO_SIMULADO = os.environ.get("CHATBOT_MODO_SIMULADO", "1") == "1"
TAMANHO_MAXIMO_PERGUNTA = 500
CACHE_SEGUNDOS = int(os.environ.get("CHATBOT_CACHE_HORAS", "24")) * 3600
BUCKET = os.environ.get("CHATBOT_BUCKET", "farmazids-t1-equipe1")
COLETA_SEGUNDOS = 600  # de quanto em quanto tempo conferir no S3 se chegou coleta nova
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
    return {"ok": False, "resposta": mensagem, "sql": None, "colunas": [], "linhas": [], "tipo": None,
            "grafico": None}


def _normalizar(pergunta):
    """"Qual o preço  da Dipirona?" e "qual o preço da dipirona" viram a mesma chave."""
    return " ".join(pergunta.lower().split()).rstrip("?!. ")


def _chave_cache(pergunta, historico, modo):
    """A mesma pergunta pode ter respostas diferentes conforme a conversa:
    "e na São João?" depois de dipirona não é o mesmo que depois de amoxicilina.
    Por isso a chave leva também as perguntas e os SQLs anteriores, e o modo
    (a resposta rápida é outra)."""
    partes = [_normalizar(troca["pergunta"]) + "\n" + " ".join((troca.get("sql") or "").lower().split())
              for troca in historico]
    return "\n--\n".join([modo] + partes + [_normalizar(pergunta)])


_coleta = {"data": None, "conferida": 0.0}


def ultima_coleta():
    """Data ("2026-10-21") da coleta mais recente no S3 (pastas processed/<farmácia>/dt=...),
    conferida no máximo a cada COLETA_SEGUNDOS. None se não der para ler (sem permissão
    s3:ListBucket, por exemplo): aí o cache vale só pelas 24 h."""
    if MODO_SIMULADO:
        return "2026-10-21"
    if time.time() - _coleta["conferida"] < COLETA_SEGUNDOS:
        return _coleta["data"]
    _coleta["conferida"] = time.time()
    try:
        datas = set()
        for pagina in _cliente_s3().get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix="processed/"):
            for objeto in pagina.get("Contents", []):
                achado = re.search(r"/dt=(\d{4}-\d{2}-\d{2})/", objeto["Key"])
                if achado:
                    datas.add(achado.group(1))
        _coleta["data"] = max(datas) if datas else None
    except Exception as erro:  # sem permissão ou sem rede: segue sem a data
        print(f"[chatbot] não li a data da coleta no S3: {erro}")
        _coleta["data"] = None
    return _coleta["data"]


# ---------- Pré-aquecimento dos atalhos ----------
# Quando o app inicia (e quando chega coleta nova), os atalhos da coluna lateral rodam uma vez em
# segundo plano, no modo completo: quem clicar já recebe do cache (o modo rápido sai da mesma resposta,
# ver _rapida_do_cache). No máximo PREAQUECER_MAXIMO execuções por vez, sem repetir as que já estão no
# cache. Se alguém clicar num atalho que ainda está aquecendo, espera essa execução em vez de abrir outra.
PREAQUECER_MAXIMO = 5
_aquecendo = {}  # chave do cache -> threading.Event (execução em andamento)
# As perguntas dos atalhos (normalizadas): o SQL delas é fixo, não depende da conversa. Então vão sem o
# histórico, e o mesmo clique acha a resposta no cache em qualquer ponto da conversa.
_perguntas_fixas = set()
_aquecimento = {"coleta": None, "feito": False, "conferido": 0.0, "rodando": False}
_trava_aquecer = threading.Lock()


def preaquecer(perguntas):
    """Chamada a cada execução da tela; não espera nada: no máximo dispara uma thread, que confere a data
    da coleta (a cada COLETA_SEGUNDOS) e roda os atalhos que faltam no cache."""
    _perguntas_fixas.update(_normalizar(p) for p in perguntas)
    if CACHE_SEGUNDOS <= 0:
        return
    with _trava_aquecer:
        if _aquecimento["rodando"] or (_aquecimento["feito"]
                                       and time.time() - _aquecimento["conferido"] < COLETA_SEGUNDOS):
            return
        _aquecimento.update(rodando=True, conferido=time.time())
    threading.Thread(target=_aquecer, args=(list(perguntas)[:PREAQUECER_MAXIMO],), daemon=True,
                     name="preaquecer-atalhos").start()


def _aquecer(perguntas):
    try:
        coleta = ultima_coleta()
        if _aquecimento["feito"] and coleta == _aquecimento["coleta"]:
            return  # mesma coleta: já aquecido
        pendentes = []
        with _trava_aquecer:
            for pergunta in perguntas:
                chave = _chave_cache(pergunta, [], "completa")
                if chave not in _aquecendo and _buscar_no_cache(chave) is None:
                    _aquecendo[chave] = threading.Event()
                    pendentes.append((pergunta, chave))
        inicio = time.perf_counter()
        print(f"[chatbot] pré-aquecimento: {len(pendentes)} atalho(s), coleta {coleta}")

        def rodar(item):
            pergunta, chave = item
            try:
                resultado = _executar(pergunta, [], "completa")
                _guardar_no_cache(chave, resultado)
                return resultado["ok"]
            finally:
                _aquecendo.pop(chave).set()

        if pendentes:
            with ThreadPoolExecutor(max_workers=len(pendentes)) as executor:
                certos = sum(executor.map(rodar, pendentes))
            print(f"[chatbot] pré-aquecimento: {certos}/{len(pendentes)} certo(s) em "
                  f"{time.perf_counter() - inicio:.1f} s")
        _aquecimento.update(coleta=coleta, feito=True)
    except Exception as erro:  # o pré-aquecimento nunca pode derrubar o app
        print(f"[chatbot] pré-aquecimento falhou: {erro!r}")
    finally:
        _aquecimento["rodando"] = False


def _esperar_aquecimento(chave, segundos=60):
    """Se essa pergunta está sendo aquecida agora, espera ela terminar (no máximo `segundos`)."""
    evento = _aquecendo.get(chave)
    if evento is not None:
        evento.wait(segundos)


def _rapida_do_cache(pergunta, historico):
    """Modo rápido de uma pergunta que já tem a resposta completa no cache: a frase sai das mesmas
    colunas e linhas (como a redigir-resposta faz no modo rápido), sem chamar a AWS."""
    chave = _chave_cache(pergunta, historico, "completa")
    _esperar_aquecimento(chave)
    completa = _buscar_no_cache(chave)
    if not completa or completa.get("tipo") != "dados" or not completa.get("linhas"):
        return None
    try:
        completa.update(resposta=_redigir_rapido()(completa["colunas"], completa["linhas"]), grafico=None, fala=None)
    except Exception as erro:
        print(f"[chatbot] frase rápida local falhou: {erro!r}")
        return None
    return completa


def _buscar_no_cache(chave):
    if CACHE_SEGUNDOS <= 0:
        return None
    coleta = ultima_coleta()
    with _trava_cache:
        guardado = _cache.get(chave)
        if guardado is None:
            return None
        momento, coleta_da_resposta, resultado = guardado
        # Sai do cache depois de 24 h ou quando chega uma coleta nova.
        if time.time() - momento > CACHE_SEGUNDOS or (coleta and coleta != coleta_da_resposta):
            del _cache[chave]
            return None
        return dict(resultado)


def _guardar_no_cache(chave, resultado):
    # Só guarda resposta certa: erro não pode ficar "preso" no cache.
    if CACHE_SEGUNDOS <= 0 or not resultado["ok"]:
        return
    coleta = ultima_coleta()
    with _trava_cache:
        if len(_cache) >= CACHE_MAXIMO:
            mais_antiga = min(_cache, key=lambda k: _cache[k][0])
            del _cache[mais_antiga]
        _cache[chave] = (time.time(), coleta, dict(resultado))


@lru_cache(maxsize=1)
def _cliente_s3():
    import boto3
    from botocore.config import Config

    return boto3.client("s3", region_name=REGIAO,
                        config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 1}))


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


def _sem_acento(texto):
    texto = unicodedata.normalize("NFKD", texto.lower().strip().rstrip("?").strip())
    return "".join(c for c in texto if not unicodedata.combining(c))


# Os atalhos da coluna lateral (pela pergunta exata, como na gerar-sql), nas colunas do SQL pronto.
_PRODUTOS = [("Dipirona Monoidratada 500mg/mL Gotas 20mL", "12.9", "7.49", "sao_joao"),
             ("Losartana Potássica 50mg 30 Comprimidos", "25.0", "16.6", "farmaponte"),
             ("Paracetamol 750mg 20 Comprimidos", "10.4", "8.19", "veracruz"),
             ("Omeprazol 20mg 28 Cápsulas", "18.5", "15.9", "sao_paulo"),
             ("Ibuprofeno 400mg 10 Cápsulas Gelatinosas Moles", "14.9", "13.2", "sao_joao"),
             ("Amoxicilina 500mg 21 Cápsulas", "32.9", "30.1", "farmaponte"),
             ("Sinvastatina 20mg 30 Comprimidos", "19.9", "18.9", "veracruz")]
_ATALHOS_SIMULADOS = {
    "em quais produtos a farmazids esta mais cara que a concorrencia": (
        "A maior diferença é na Dipirona Monoidratada 500mg/mL Gotas 20mL: a Farmazids cobra R$ 12,90 e a São "
        "João, R$ 7,49 (72% acima). Três produtos com diferença acima de 200% ficaram de fora, por serem "
        "provavelmente erro de cadastro.",
        ["produto", "farmazids_rs", "menor_concorrente_rs", "farmacia", "diferenca", "acima_pct", "ignorados_acima_200"],
        [[n, f, c, fa, f"{float(f) - float(c):.2f}", f"{(float(f) - float(c)) / float(c) * 100:.1f}", "3"]
         for n, f, c, fa in _PRODUTOS]),
    "em quais produtos a farmazids esta mais barata que a concorrencia": (
        "A Farmazids está mais barata principalmente na Atorvastatina 20mg 30 Comprimidos: R$ 29,90, contra "
        "R$ 41,50 na FarmaPonte (28% abaixo).",
        ["produto", "farmazids_rs", "menor_concorrente_rs", "farmacia", "diferenca", "abaixo_pct", "ignorados_acima_200"],
        [["Atorvastatina 20mg 30 Comprimidos", "29.9", "41.5", "farmaponte", "11.6", "28.0", "1"],
         ["Loratadina 10mg 12 Comprimidos", "9.9", "12.4", "sao_joao", "2.5", "20.2", "1"],
         ["Dorflex 36 Comprimidos", "21.9", "24.9", "veracruz", "3.0", "12.0", "1"]]),
    "quais os 10 produtos mais vendidos da farmazids nos ultimos 3 meses e como esta o preco deles na concorrencia": (
        "O mais vendido foi a Dipirona Monoidratada 500mg/mL Gotas 20mL, com 412 unidades; nele a Farmazids "
        "está 72% acima do menor preço da concorrência (São João).",
        ["produto", "unidades", "farmazids_rs", "menor_concorrente_rs", "farmacia", "diferenca_pct"],
        [[n, str(u), f, c, fa, f"{(float(f) - float(c)) / float(c) * 100:.1f}"]
         for (n, f, c, fa), u in zip(_PRODUTOS, (412, 380, 351, 290, 244, 198, 150))]
        + [["Código 7891234567895", "120", None, None, None, None]]),
    "quantas unidades cada farmacia vendeu por mes nos ultimos 6 meses": (
        "Em setembro de 2026, a FarmaPonte vendeu 1.320 unidades e a VeraCruz, 980.",
        ["farmacia", "mes", "unidades"],
        [[f, m, str(u)] for f, base in (("farmaponte", 1200), ("veracruz", 900))
         for i, m in enumerate(("2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"))
         for u in (base + 24 * i,)]),
    "quais produtos da farmazids mais cairam em vendas nos ultimos 3 meses": (
        "A maior queda foi na Losartana Potássica 50mg 30 Comprimidos: de 210 para 95 unidades (55% a menos), "
        "e a Farmazids cobra R$ 25,00, contra R$ 16,60 na FarmaPonte.",
        ["produto", "unidades_antes", "unidades_agora", "queda_pct", "farmazids_rs", "menor_concorrente_rs", "farmacia"],
        [[n, str(a), str(d), f"{(a - d) / a * 100:.1f}", f, c, fa]
         for (n, f, c, fa), (a, d) in zip(_PRODUTOS[1:5], ((210, 95), (180, 120), (88, 61), (40, 33)))]),
}


def _perguntar_simulado(pergunta, historico):
    time.sleep(1.5)  # imita o tempo de resposta do backend
    atalho = _ATALHOS_SIMULADOS.get(_sem_acento(pergunta))
    if atalho:
        resposta, colunas, linhas = atalho
        return {"ok": True, "resposta": resposta, "sql": "WITH fz AS (...), conc AS (...) ... (SQL pronto do atalho)",
                "colunas": colunas, "linhas": linhas, "tipo": "dados", "grafico": None}
    contexto = f" (com {len(historico)} troca(s) anterior(es) no histórico)" if historico else ""
    pediu_grafico = "gráfico" in pergunta.lower() or "grafico" in pergunta.lower()
    if any(p in pergunta.lower() for p in ("vend", "fatur", "arrecad")):
        # Para testar as vendas (a base não tem preço; o produto vem do código de barras).
        # Com "dipirona" na pergunta (filtro de remédio): o total conta só a dipirona identificada, e as
        # vendas com código sem cadastro vêm à parte (o caso do cliente: FarmaPonte em janeiro, 1 venda
        # de dipirona e 12 com código sem cadastro). Sem remédio: vendas totais, com elas no total.
        # Com "arrecadado" na pergunta, o texto vem do jeito errado, para testar a correção da tela.
        texto = pergunta.lower()
        fora = any(f in texto for f in ("são joão", "sao joao", "são paulo", "sao paulo"))
        dentro = any(f in texto for f in ("farmaponte", "farma ponte", "veracruz", "vera cruz", "farmazids"))
        if fora and not dentro:
            # Como na máquina: a gerar-sql devolve CANNOT_ANSWER (a base de vendas não tem essa farmácia).
            return {"ok": True, "resposta": "Não encontrei nos dados disponíveis informação para responder a "
                    "essa pergunta. Tente reformular ou perguntar sobre outro assunto.", "sql": "",
                    "colunas": [], "linhas": [], "tipo": "sem_dados", "grafico": None}
        if "sem cadastro" in texto and any(f in texto for f in ("liste", "lista", "quais")):
            # Lista dos códigos sem cadastro: a pergunta é sobre eles (nada de "valor parcial: 22 de 22").
            codigos = [("7891234567895", "FarmaPonte", 9), ("7896004700012", "FarmaPonte", 8),
                       ("7899095200059", "VeraCruz", 5)]
            return {"ok": True, "resposta": "Estes são os códigos de barras sem cadastro nas vendas: 3 códigos, "
                    "em 22 vendas. Sem cadastro, não dá para saber qual é o produto nem estimar o valor.",
                    "sql": "WITH vendas AS (...) ... WHERE preco IS NULL GROUP BY ean, farmacia ... LIMIT 50",
                    "colunas": ["ean", "farmacia", "total_vendas", "vendas_com_preco", "vendas_sem_preco",
                                "valor_estimado"],
                    "linhas": [[c, f, str(n), "0", str(n), None] for c, f, n in codigos],
                    "tipo": "dados", "grafico": None}
        if "sem cadastro" in texto:
            # Vendas totais por farmácia e mês, com a coluna dos códigos sem cadastro e SEM remédio filtrado.
            dados = {"FarmaPonte": [(140, 128, 2210.5, 12), (131, 129, 2045.9, 2)],
                     "VeraCruz": [(98, 97, 1530.2, 1), (102, 101, 1610.8, 1)]}
            return {"ok": True, "resposta": "Em janeiro, a FarmaPonte teve 140 vendas, 12 delas com código de "
                    "barras sem cadastro; a VeraCruz teve 98, com 1 sem cadastro. O valor é estimado com os "
                    "preços atuais, porque a base de vendas não tem o preço de cada venda.",
                    "sql": "WITH vendas AS (...) ... SELECT farmacia, mes, count(*) AS total_vendas, ..., "
                           "count_if(nome IS NULL) AS vendas_codigo_sem_cadastro FROM identificadas ... LIMIT 50",
                    "colunas": ["farmacia", "mes", "total_vendas", "vendas_com_preco", "vendas_sem_preco",
                                "valor_estimado", "vendas_codigo_sem_cadastro"],
                    "linhas": [[f, m, str(t), str(c), str(t - c), str(v), str(x)] for f, lista in dados.items()
                               for m, (t, c, v, x) in zip(["2026-01", "2026-02"], lista)],
                    "tipo": "dados", "grafico": None}
        if "veracruz" in texto or "vera cruz" in texto:
            # Resultado vazio, com a frase do valor estimado escrita por engano (a tela tira).
            return {"ok": True, "resposta": "Não foram encontradas vendas de dipirona na VeraCruz em janeiro de "
                    "2026. O valor é estimado com os preços atuais, porque a base de vendas não tem o preço de "
                    "cada venda.", "sql": "WITH vendas AS (...) ... LIKE '%dipirona%' ... LIMIT 50",
                    "colunas": ["farmacia", "mes", "total_vendas", "vendas_com_preco", "vendas_sem_preco",
                                "unidades", "valor_estimado", "vendas_codigo_sem_cadastro"],
                    "linhas": [], "tipo": "dados", "grafico": None}
        meses = ["2026-01", "2026-02", "2026-03"]
        filtrou = "dipirona" in pergunta.lower()
        if filtrou:
            # (vendas de dipirona, com preço, unidades, valor, vendas da farmácia no mês com código sem cadastro)
            dados = {"FarmaPonte": [(1, 1, 2, 20.0, 12), (7, 7, 9, 48.3, 2), (11, 11, 12, 61.9, 0)],
                     "VeraCruz": [(8, 8, 9, 39.6, 0), (9, 9, 10, 44.1, 1), (7, 7, 8, 35.2, 0)]}
            if fora:  # pergunta com São João e FarmaPonte: só a FarmaPonte tem vendas
                dados = {"FarmaPonte": dados["FarmaPonte"]}
            colunas = ["farmacia", "mes", "total_vendas", "vendas_com_preco", "vendas_sem_preco", "unidades",
                       "valor_estimado", "vendas_codigo_sem_cadastro"]
            linhas = [[f, m, str(t), str(c), str(t - c), str(u), str(v), str(x)]
                      for f, lista in dados.items() for m, (t, c, u, v, x) in zip(meses, lista)]
            # Repete as vendas sem cadastro, como o Bedrock fez no site (a tela tira a repetição).
            resposta = (f"Em janeiro, a FarmaPonte teve 1 venda de dipirona identificada, com valor estimado de "
                        f"R$ 20,00 (preços atuais){contexto}. Também há vendas com código de barras sem cadastro "
                        "no período. Elas aparecem assim:\n- FarmaPonte: 12 em janeiro e 2 em fevereiro\n"
                        "- VeraCruz: 1 em fevereiro\n\nO valor é estimado com os preços atuais, porque a base de "
                        "vendas não tem o preço de cada venda.")
            sql = ("WITH vendas AS (...) ... SELECT farmacia, ..., count_if(LOWER(nome) LIKE '%dipirona%') AS "
                   "total_vendas, ..., count_if(nome IS NULL) AS vendas_codigo_sem_cadastro FROM identificadas "
                   "GROUP BY 1, 2 ... LIMIT 50")
        else:
            dados = {"FarmaPonte": [(140, 128, 160, 2210.5), (131, 129, 150, 2045.9), (152, 152, 170, 2388.0)],
                     "VeraCruz": [(98, 97, 110, 1530.2), (102, 101, 115, 1610.8), (95, 95, 104, 1499.3)]}
            colunas = ["farmacia", "mes", "total_vendas", "vendas_com_preco", "vendas_sem_preco", "unidades",
                       "valor_estimado"]
            linhas = [[f, m, str(t), str(c), str(t - c), str(u), str(v)]
                      for f, lista in dados.items() for m, (t, c, u, v) in zip(meses, lista)]
            resposta = (f"Em janeiro, a FarmaPonte teve 140 vendas, com valor estimado de R$ 2.210,50 (preços "
                        f"atuais){contexto}. O valor é estimado com os preços atuais, porque a base de vendas não "
                        "tem o preço de cada venda.")
            sql = "WITH vendas AS (...) ... SELECT farmacia, ..., count(*) AS total_vendas, ... LIMIT 50"
        if "arrecad" in pergunta.lower():
            resposta = (f"Em janeiro, a Farmaponte teve 13 vendas e o valor arrecadado foi de R$ 20,00, contra "
                        f"R$ 39,60 na Sao Joao{contexto}. Além disso, 12 vendas da Farmaponte em janeiro de 2026 "
                        "têm código de barras sem cadastro; não é possível saber se alguma é de dipirona. Das 13, "
                        "12 tinham SKU sem cadastro.")
        return {
            "ok": True,
            "resposta": resposta,
            "sql": sql,
            "colunas": colunas,
            "linhas": linhas,
            "tipo": "dados",
            "grafico": {"tipo": "barras", "x": "mes", "y": ["valor_estimado"], "serie": "farmacia",
                        "pedido": True} if pediu_grafico else None,
        }
    if "diferença por unidade" in pergunta.lower():
        # Colunas cruas, para conferir os rótulos e os números formatados na tabela.
        return {"ok": True, "resposta": "A maior diferença por unidade é na Dipirona injetável.", "sql": "SELECT ...",
                "colunas": ["nome", "concentracao", "forma", "diferenca_por_unidade", "desconto_percentual", "preco_medio"],
                "linhas": [["Dipirona Injetável 2mL", "500mg/ml", "injetavel", "100.380000", "12.345", "15.5"],
                           ["Dipirona Gotas 20mL", "500mg/ml", "gotas", "0.250000", "3.1", "9.99"]],
                "tipo": "dados", "grafico": None}
    codigo = re.search(r"código de barras (\d{13}|\d{8})(?!\d)", pergunta)
    if codigo and codigo.group(1) != "7891058017392":
        # Leitor de código de barras, código válido que não está em nenhuma farmácia: resultado vazio
        # (a tela troca o texto pela explicação). Só o 7891058017392 é "encontrado" no modo simulado.
        return {"ok": True, "resposta": "Não foram encontrados dados para esse produto.",
                "sql": "WITH ultima AS (...) ... WHERE regexp_replace(trim(CAST(u.ean AS varchar)), '^0+', '') = "
                       f"'{codigo.group(1)}' ... LIMIT 50",
                "colunas": ["farmacia", "produto", "marca", "preco_por_unidade", "preco", "origem_preco",
                            "quantidade", "concentracao", "forma", "unidade", "em_estoque", "data_coleta"],
                "linhas": [], "tipo": "dados", "grafico": None}
    if "compar" in pergunta.lower() or "barat" in pergunta.lower():
        # Para testar a comparação por unidade (vw_precos_unitarios), no formato da gerar-sql:
        # paracetamol em 3 apresentações + 1 combinação, a Farmazids fora do top 3, um produto
        # sem quantidade e a resposta em lista (a tela deve usar só a primeira frase). A São João
        # vem com o preço do cartão (ela não informa Pix): a tela marca "(cartão)".
        c = "2026-10-16"

        def linha(farmacia, produto, marca, ppu, preco, qtd, conc, forma, unidade, estoque="true", coleta=c):
            origem = None if farmacia == "farmazids" else "cartao" if farmacia == "sao_joao" else "pix"
            return [farmacia, produto, marca, ppu, preco, origem, qtd, conc, forma, unidade, estoque, coleta]

        return {
            "ok": True,
            "resposta": f"- O paracetamol 750 mg sai mais barato na **FarmaPonte**, a R$ 0,34 por comprimido. "
                        f"A Farmazids fica em 5º lugar{contexto}.\n- Na apresentação de 500 mg, a São João vence.",
            # Pelo leitor, o filtro é o código de barras (sem o nome do remédio no SQL).
            "sql": ("WITH ultima AS (...) ... WHERE regexp_replace(trim(CAST(u.ean AS varchar)), '^0+', '') = "
                    "'7891058017392' ... LIMIT 50") if codigo else
                   "WITH ultima AS (...) ... WHERE LOWER(u.nome) LIKE '%paracetamol%' ... LIMIT 50",
            "colunas": ["farmacia", "produto", "marca", "preco_por_unidade", "preco", "origem_preco", "quantidade",
                        "concentracao", "forma", "unidade", "em_estoque", "data_coleta"],
            "linhas": [
                linha("farmaponte", "Paracetamol 750mg 20 Comprimidos Genérico EMS", "EMS", "0.3355", "6.71", "20.0",
                      "750mg", "comprimido", "comprimido"),
                linha("sao_joao", "Paracetamol 750mg 30 Comprimidos Medley", "Medley", "0.3357", "10.07", "30.0",
                      "750mg", "comprimido", "comprimido"),
                linha("veracruz", "Tylenol 750mg 20 Comprimidos", "Kenvue", "0.4495", "8.99", "20.0", "750mg",
                      "comprimido", "comprimido", "false"),
                linha("sao_paulo", "Paracetamol 750mg 10 Comprimidos Neo Química", "Neo Química", "0.499", "4.99",
                      "10.0", "750mg", "comprimido", "comprimido"),
                linha("farmazids", "Paracetamol 750mg 20 Comprimidos Prati-Donaduzzi", None, "0.52", "10.4", "20.0",
                      "750mg", "comprimido", "comprimido", None, None),
                linha("sao_joao", "Paracetamol 500mg 20 Comprimidos Genérico EMS", "EMS", "0.12", "2.4", "20.0",
                      "500mg", "comprimido", "comprimido"),
                linha("farmaponte", "Paracetamol 500mg 10 Comprimidos Medley", "Medley", "0.199", "1.99", "10.0",
                      "500mg", "comprimido", "comprimido"),
                linha("farmaponte", "Paracetamol Gotas 200mg/ml 15ml", "Prati-Donaduzzi", "0.42", "6.3", "15.0",
                      "200mg/ml", "gotas", "ml"),
                linha("veracruz", "Tylenol Gotas 200mg/ml 15ml", "Kenvue", "1.26", "18.9", "15.0", "200mg/ml",
                      "gotas", "ml"),
                linha("sao_joao", "Tylex Paracetamol 500mg + Codeína 30mg 12 Comprimidos", "Cellera", "3.325",
                      "39.9", "12.0", "500mg+30mg", "comprimido", "comprimido"),
                linha("farmaponte", "Tylex Paracetamol 500mg + Codeína 30mg 24 Comprimidos", "Cellera", "2.9",
                      "69.6", "24.0", "500mg+30mg", "comprimido", "comprimido"),
                linha("sao_paulo", "Paracetamol 750mg Comprimidos", "nan", None, "7.9", None, "750mg", "comprimido",
                      None, "None", "NaT"),
            ],
            "tipo": "dados",
            "grafico": {"tipo": "barras", "x": "farmacia", "y": ["preco_por_unidade"], "serie": None,
                        "pedido": True} if pediu_grafico else None,
        }
    if "evolu" in pergunta.lower():
        # Para testar o gráfico de linha: menor preço por coleta em três farmácias.
        datas = ["2026-10-06", "2026-10-08", "2026-10-10", "2026-10-12", "2026-10-14", "2026-10-16"]
        precos = {"farmaponte": [4.59, 4.49, 4.49, 4.39, 4.29, 4.29],
                  "sao_joao": [4.39, 4.39, 4.29, 4.19, 4.19, 4.19],
                  "sao_paulo": [5.10, 4.99, 4.99, 4.89, 4.79, 4.89]}
        return {
            "ok": True,
            "resposta": f"(modo simulado) Evolução do menor preço da dipirona{contexto}. O gráfico mostra "
                        "o menor preço encontrado em cada coleta, entre vários produtos.",
            "sql": "SELECT date(data_coleta) AS data_coleta, farmacia, MIN(preco_comparacao) AS menor_preco,\n"
                   "min_by(preco_comparacao_origem, preco_comparacao) AS origem_preco\n"
                   "FROM vw_precos_unitarios\n"
                   "WHERE lower(nome) LIKE '%dipirona%'\n"
                   "GROUP BY 1, 2 ORDER BY 1 LIMIT 50",
            # A São João vem com o preço do cartão (ela não informa Pix): o gráfico marca "(cartão)".
            "colunas": ["data_coleta", "farmacia", "menor_preco", "origem_preco"],
            "linhas": [[data, farmacia, str(lista[i]), "cartao" if farmacia == "sao_joao" else "pix"]
                       for farmacia, lista in precos.items() for i, data in enumerate(datas)],
            "tipo": "dados",
            # Como na redigir-resposta: só há gráfico quando a pergunta pede.
            "grafico": {"tipo": "linha", "x": "data_coleta", "y": ["menor_preco"], "serie": "farmacia",
                        "pedido": True} if pediu_grafico else None,
        }
    return {
        "ok": True,
        "resposta": f"(modo simulado) Você perguntou: \"{pergunta}\"{contexto}. "
                    "Quando o backend estiver ligado, a resposta de verdade aparece aqui.",
        "fala": "Este é o modo de teste. Na São João, a dipirona de 500 miligramas sai por 4 reais e 19 centavos.",
        "sql": "SELECT farmacia, nome, preco_pix, preco_cartao, em_estoque, data_coleta\n"
               "FROM vw_precos_concorrentes\n"
               "WHERE lower(nome) LIKE '%dipirona%'\n"
               "LIMIT 10",
        "colunas": ["farmacia", "nome", "preco_pix", "preco_cartao", "em_estoque", "data_coleta"],
        "linhas": [
            ["sao_joao", "Dipirona Monoidratada 500mg 10 Comprimidos", "4.19", "4.49", "true", "2026-10-16"],
            ["sao_joao", "Dipirona 1g 10 Comprimidos", "9.90", "10.49", "true", "2026-10-16"],
            ["sao_joao", "Novalgina Gotas 20ml", "18.90", "19.90", "true", "2026-10-16"],
            ["farmaponte", "Dipirona Monoidratada 500mg 10 Comprimidos", "4.29", "4.79", "true", "2026-10-16"],
            ["farmaponte", "Dipirona Gotas 500mg/ml 20ml", "8.49", "8.99", "true", "2026-10-16"],
            ["farmaponte", "Novalgina 1g 10 Comprimidos", "22.90", "24.50", "true", "2026-10-16"],
            ["veracruz", "Dipirona Gotas 500mg/ml 20ml", "7.90", "8.49", "false", "2026-10-16"],
            ["sao_paulo", "Novalgina 1g 10 Comprimidos", "21.99", "23.90", "true", "2026-10-16"],
            ["sao_paulo", "Dipirona 1g 10 Comprimidos", "9999876", "9999876", "false", "2026-10-16"],
        ],
        "tipo": "dados",
        # Várias linhas por farmácia, como no resultado real: o gráfico mostra o
        # menor preço de cada uma, nunca a soma. O 9999876 (fora de estoque na
        # São Paulo) está aí de propósito: a tela tira do gráfico.
        "grafico": {"tipo": "barras", "x": "farmacia", "y": ["preco_pix", "preco_cartao"], "serie": None,
                    "pedido": True} if pediu_grafico else None,
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
        # Sugestão de gráfico da redigir-resposta. A máquina antiga não manda: fica None.
        "grafico": saida.get("grafico"),
        # A resposta escrita para ser ouvida (botão "Ouvir"). A máquina antiga não manda: fica None.
        "fala": saida.get("fala"),
    }


def _executar(pergunta, historico, modo):
    """Uma execução de verdade (Step Functions; no modo simulado, a resposta de teste)."""
    if MODO_SIMULADO:
        resultado = _perguntar_simulado(pergunta, historico)
        if modo == "rapida" and resultado.get("tipo") == "dados":
            # Como a redigir-resposta no modo rápido: a frase sai dos dados, sem Bedrock.
            resultado.update(resposta=_redigir_rapido()(resultado["colunas"], resultado["linhas"]),
                             grafico=None, fala=None)
        return resultado
    # O modo rápido vai como último item do histórico (a máquina só repassa o histórico).
    return _perguntar_aws(pergunta, historico + ([{"modo": "rapida"}] if modo == "rapida" else []))


def perguntar(pergunta, historico=None, modo="completa"):
    """Recebe a pergunta, o histórico (lista de {"pergunta", "resposta", "sql"},
    da troca mais antiga para a mais recente; vazio na primeira pergunta) e o modo
    ("completa" ou "rapida") e devolve sempre as mesmas chaves:
    ok, resposta, sql, colunas, linhas, tipo, grafico, duracao (segundos) e do_cache."""
    inicio = time.perf_counter()
    pergunta = (pergunta or "").strip()
    historico = [] if _normalizar(pergunta) in _perguntas_fixas else (historico or [])
    if not pergunta:
        resultado = _falha("Escreva uma pergunta.")
    elif len(pergunta) > TAMANHO_MAXIMO_PERGUNTA:
        resultado = _falha(f"A pergunta pode ter no máximo {TAMANHO_MAXIMO_PERGUNTA} caracteres.")
    else:
        chave = _chave_cache(pergunta, historico, modo)
        _esperar_aquecimento(chave)
        guardado = _buscar_no_cache(chave)
        if guardado is None and modo == "rapida":
            guardado = _rapida_do_cache(pergunta, historico)
        if guardado is not None:
            guardado["do_cache"] = True
            guardado["duracao"] = time.perf_counter() - inicio
            print(f"[chatbot] {guardado['duracao']:.1f} s | do cache | modo={modo} | {pergunta[:60]}")
            return guardado
        resultado = _executar(pergunta, historico, modo)
        _guardar_no_cache(chave, resultado)

    resultado["do_cache"] = False
    resultado["duracao"] = time.perf_counter() - inicio
    # Fica no log (terminal ou "Manage app" no Streamlit Cloud): ajuda a medir o tempo real.
    # O SQL não entra no log; as colunas sim (mostram se veio o preço por unidade, que dá a
    # resposta em cartões).
    print(f"[chatbot] {resultado['duracao']:.1f} s | ok={resultado['ok']} | "
          f"historico={len(historico)} | modo={modo} | {pergunta[:60]} | colunas={','.join(resultado['colunas'])}")
    return resultado


@lru_cache(maxsize=1)
def _redigir_rapido():
    """Modo simulado: a mesma frase rápida da redigir-resposta (src/lambdas), sem chamar a AWS."""
    import importlib.util
    from pathlib import Path

    caminho = Path(__file__).resolve().parents[1] / "lambdas" / "redigir_resposta.py"
    spec = importlib.util.spec_from_file_location("redigir_resposta_local", caminho)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo.frase_rapida


# ---------- Voz: Amazon Polly ----------
@lru_cache(maxsize=1)
def _cliente_polly():
    """Mesmas credenciais e região do Step Functions."""
    import boto3
    from botocore.config import Config

    return boto3.client("polly", region_name=REGIAO,
                        config=Config(connect_timeout=3, read_timeout=10, retries={"max_attempts": 1}))


def _audio_de_teste(ssml):
    """Modo simulado: um WAV silencioso, mais longo quanto maior o texto (para testar o "Parar")."""
    segundos = min(6.0, 0.5 + len(ssml) / 200)
    saida = io.BytesIO()
    with wave.open(saida, "wb") as arquivo:
        arquivo.setnchannels(1)
        arquivo.setsampwidth(2)
        arquivo.setframerate(8000)
        arquivo.writeframes(b"\x00\x00" * int(8000 * segundos))
    return saida.getvalue(), "audio/wav"


def sintetizar(ssml, voz):
    """Áudio da fala pelo Amazon Polly (voz neural pt-BR). Devolve (bytes, tipo do áudio).
    Levanta exceção se o Polly falhar: quem chama decide o que fazer (a tela usa a voz do navegador)."""
    if MODO_SIMULADO:
        if os.environ.get("CHATBOT_POLLY_SIMULADO", "ok") == "falha":
            raise RuntimeError("Polly simulado: falha de propósito (CHATBOT_POLLY_SIMULADO=falha)")
        return _audio_de_teste(ssml)
    resposta = _cliente_polly().synthesize_speech(
        Engine="neural", LanguageCode="pt-BR", VoiceId=voz, OutputFormat="mp3", TextType="ssml", Text=ssml)
    return resposta["AudioStream"].read(), "audio/mpeg"
