"""Faz a ponte entre a tela (Streamlit) e o Step Functions do chatbot.

Dois modos:
- simulado (padrão): devolve uma resposta de teste, sem chamar a AWS.
  Serve para montar e testar a tela antes de ligar no backend.
- real: chama a máquina Expressa do Step Functions e espera a resposta.

Configuração por variáveis de ambiente (nada de credencial no código):
- CHATBOT_MODO_SIMULADO = "1" (simulado) ou "0" (real)
- CHATBOT_STATE_MACHINE_ARN = ARN da máquina Expressa
- CHATBOT_CACHE_MINUTOS = por quantos minutos guardar uma resposta (padrão 30; 0 desliga)
- CHATBOT_POLLY_SIMULADO = no modo simulado, "ok" (padrão: um áudio de teste) ou "falha"
  (finge que o Polly falhou, para testar a volta para a voz do navegador)

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

import io
import json
import os
import threading
import time
import wave
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
    return {"ok": False, "resposta": mensagem, "sql": None, "colunas": [], "linhas": [], "tipo": None,
            "grafico": None}


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
    pediu_grafico = "gráfico" in pergunta.lower() or "grafico" in pergunta.lower()
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
            "sql": "WITH ultima AS (...) ... WHERE LOWER(u.nome) LIKE '%paracetamol%' ... LIMIT 50",
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


def perguntar(pergunta, historico=None):
    """Recebe a pergunta e o histórico (lista de {"pergunta", "resposta", "sql"},
    da troca mais antiga para a mais recente; vazio na primeira pergunta) e
    devolve sempre as mesmas chaves:
    ok, resposta, sql, colunas, linhas, tipo, grafico, duracao (segundos) e do_cache."""
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
    # O SQL não entra no log; as colunas sim (mostram se veio o preço por unidade, que dá a
    # resposta em cartões).
    print(f"[chatbot] {resultado['duracao']:.1f} s | ok={resultado['ok']} | "
          f"historico={len(historico)} | {pergunta[:60]} | colunas={','.join(resultado['colunas'])}")
    return resultado


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
