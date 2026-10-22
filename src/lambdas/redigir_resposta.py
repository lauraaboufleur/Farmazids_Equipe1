"""Agente 4 - Redigir resposta.

Recebe:  {"pergunta": "...", "colunas": ["..."], "linhas": [[...], ...], "historico": [...]}
         historico é opcional: lista de {"pergunta", "resposta", "sql"} das últimas
         trocas, da mais antiga para a mais recente. Aqui só entram a pergunta e a
         resposta resumida. Sem histórico, funciona como antes.
Devolve: {"resposta": "...", "grafico": {...} ou null, "fala": "..." ou null}
         grafico é a sugestão de gráfico para a tela:
         {"tipo": "barras" | "linha", "x": coluna, "y": [colunas de valores],
          "serie": coluna que separa as linhas (ou null), "pedido": true}.
         Só há gráfico quando a pergunta pede ("mostra em gráfico", "faz um
         gráfico"...). Sem pedido, o modelo nem recebe as instruções de gráfico
         (economiza tokens) e grafico vem null. A chave vem sempre.
         fala é a mesma resposta escrita para ser ouvida (botão "Ouvir" da tela).
         Vem null nas comparações por unidade, porque nelas a tela monta a fala com
         os números do ranking. A chave também vem sempre (a máquina lê $.Payload.fala).
Modo rápido: a tela manda {"modo": "rapida"} como último item do historico (a máquina só
repassa pergunta, historico, colunas e linhas). Aí a resposta é 1 ou 2 frases montadas aqui,
com os dados, sem chamar o Bedrock (mais rápido e mais barato), sem gráfico e sem fala.
"""
import os
import re
import time

import boto3
from botocore.config import Config

REGION = os.environ.get("AWS_REGION", "us-east-2")
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-haiku-5-5")
LINHAS_NO_PROMPT = int(os.environ.get("LINHAS_NO_PROMPT", "100"))
# A resposta é curta (algumas frases, a fala e, às vezes, o gráfico): 800 tokens sobram e cortam
# uma resposta desembestada. O prompt (~850 tokens) fica abaixo do mínimo do prompt caching.
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "800"))
# A tela já manda no máximo 3 trocas; o corte aqui é só uma garantia.
TROCAS_NO_HISTORICO = 3
TIPOS_GRAFICO = {"barras", "linha"}
MAXIMO_VALORES = 3  # quantas colunas de valores um gráfico pode ter
PEDIU_GRAFICO = re.compile(r"gr[áa]fico", re.IGNORECASE)  # a tela confere com a mesma regra
PRECO_POR_UNIDADE = re.compile(r"preco(_\w+)?_por_unidade$", re.IGNORECASE)  # comparação por unidade

bedrock = boto3.client(
    "bedrock-runtime", region_name=REGION, config=Config(retries={"max_attempts": 4, "mode": "adaptive"})
)

SYSTEM = (
    "Você é o assistente de dados da Farmazids. Responda em português, de forma clara e curta, "
    "usando somente os dados fornecidos. Se o resultado estiver vazio, diga que não foram encontrados "
    "dados. Não invente números. O usuário também verá a tabela completa, então não a repita inteira.\n"
    "Escreva em português natural: nunca nomes de colunas nem campos crus (ex.: 'concentracao: 200mg/ml; "
    "forma: injetavel', 'preco_farmazids'); diga 'o preço da Farmazids', '200 mg/ml', 'injetável'. Valores "
    "em reais sempre como R$ 1.234,56; nunca números crus como 100.380000.\n"
    "Farmazids x concorrentes (colunas preco_farmazids, menor_preco_concorrente e diferenca): é o mesmo "
    "produto nas duas pontas. Diga em 1 ou 2 frases onde a diferença é maior, em reais, sem percentual.\n\n"
    "Nomes das farmácias: nos dados elas podem aparecer como códigos. Na resposta, escreva sempre "
    "exatamente assim: FarmaPonte, VeraCruz, São João, Drogaria São Paulo e Farmazids (nunca Farmaponte, "
    "Vera Cruz, São Paulo sozinho nem os códigos como sao_joao).\n"
    "Se aparecer o preço 9999876, ele não é um preço real: significa produto fora de estoque na São Paulo.\n"
    "Se vier uma conversa anterior e a pergunta atual for continuação dela, responda de forma que faça "
    "sentido na sequência (ex.: 'Na São João, a mesma dipirona custa...'). Use somente os números do "
    "resultado atual, nunca os da conversa anterior.\n"
    "Se a pergunta também pedir informação geral sobre o remédio (ex.: 'para que serve a dipirona e "
    "quanto custa?'), responda primeiro a parte dos dados e depois acrescente no máximo 2 frases gerais "
    "sobre o remédio, sem indicar dose, tratamento ou diagnóstico, terminando com: 'Para orientação "
    "sobre o seu caso, procure um farmacêutico ou médico.'\n"
    "Comparação por unidade: se o resultado tiver preço por unidade (colunas como preco_por_unidade ou "
    "preco_pix_por_unidade), a tela já mostra o ranking, as outras apresentações e a tabela. Então a "
    "resposta é UMA frase só, sem listas, sem marcadores, sem quebras de linha, sem markdown e sem "
    "títulos: quem é mais barato por unidade na apresentação (concentração e forma) vendida pelo maior "
    "número de farmácias, ou na que a pergunta pediu, com o preço por unidade. Exemplo: 'O paracetamol "
    "750 mg sai mais barato na FarmaPonte, a R$ 0,34 por comprimido.' Ignore os remédios combinados "
    "(concentração com '+', ex.: '500mg+30mg'), a menos que a pergunta cite a combinação. Preço por "
    "unidade com 2 casas decimais. Nada além dessa frase.\n"
    "Vendas: se o resultado tiver valor_estimado, chame esse valor de 'valor estimado (preços atuais)', "
    "nunca de 'arrecadado' nem 'faturado', e inclua uma frase curta dizendo que ele é estimado com os "
    "preços atuais, porque a base de vendas não tem o preço de cada venda. Não fale das vendas sem produto "
    "identificado nem das vendas com código de barras sem cadastro (colunas vendas_sem_preco e "
    "vendas_codigo_sem_cadastro): a tela já mostra esses números num aviso. A coluna "
    "vendas_codigo_sem_cadastro não entra no total nem no valor. Nunca use a palavra SKU: diga produto ou "
    "código de barras.\n\n"
    "Entregue sempre pela ferramenta responder."
)

# Só vai para o modelo quando não é comparação por unidade (aí a tela monta a fala).
INSTRUCOES_FALA = (
    "\n\nFala: além da resposta escrita, escreva no campo fala a mesma resposta para ser ouvida, como numa "
    "conversa: curta (no máximo 2 frases), sem tabelas, listas, símbolos nem siglas, com números e unidades "
    "por extenso do jeito que se fala (ex.: '34 centavos', '10 reais e 7 centavos', '500 miligramas', "
    "'quinto lugar'; nunca 'R$', 'mg' ou 'ml')."
)

# Só vai para o modelo quando a pergunta pede gráfico.
INSTRUCOES_GRAFICO = (
    "\n\nGráfico: a pergunta pediu um gráfico. Além da resposta, sugira o gráfico que melhor mostra o "
    "resultado.\n"
    "- barras: comparação entre farmácias ou entre produtos (x = a coluna que nomeia cada barra, como "
    "farmacia; y = 1 a 3 colunas de valores, como preco_pix e preco_cartao).\n"
    "- linha: evolução ao longo das datas de coleta (x = a coluna de data; y = uma coluna de valor; "
    "serie = a coluna que separa as linhas, como farmacia, quando houver mais de uma).\n"
    "- null: resultado de um valor só, menos de 2 linhas ou lista de produtos sem comparação. Nesse "
    "caso, explique em uma frase na resposta por que não dá para mostrar em gráfico.\n"
    "- Use só nomes de colunas que aparecem no resultado, escritos exatamente como lá.\n"
    "- Se sugerir barras e o resultado tiver várias linhas para o mesmo valor de x (ex.: vários produtos "
    "por farmácia), diga claramente na resposta que o gráfico compara o menor preço encontrado em cada "
    "farmácia (ou em cada valor de x). O gráfico nunca soma preços.\n"
    "- Se o resultado for o menor preço por coleta de uma busca genérica (ex.: coluna menor_preco_pix, "
    "sem um produto único), diga claramente na resposta que o gráfico mostra o menor preço encontrado "
    "em cada coleta, entre vários produtos."
)

# A ferramenta obriga o modelo a devolver a resposta (e o gráfico, quando pedido)
# separados, já em JSON, sem precisar procurar o JSON no meio do texto.
RESPOSTA = {"type": "string", "description": "Texto da resposta, em português."}
FALA = {"type": "string", "description": "A mesma resposta escrita para ser ouvida, curta e com números por extenso."}
GRAFICO = {
    "type": ["object", "null"],
    "description": "Sugestão de gráfico, ou null quando o resultado não serve para gráfico.",
    "properties": {
        "tipo": {"type": "string", "enum": sorted(TIPOS_GRAFICO)},
        "x": {"type": "string"},
        "y": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": MAXIMO_VALORES},
        "serie": {"type": ["string", "null"]},
    },
    "required": ["tipo", "x", "y"],
}


# ---------- Modo rápido (sem Bedrock) ----------
NOMES_FARMACIAS = {"farmaponte": "FarmaPonte", "veracruz": "VeraCruz", "sao_joao": "São João",
                   "sao_paulo": "Drogaria São Paulo", "farmazids": "Farmazids"}
CONTAGENS = {"total_vendas", "vendas_com_preco", "vendas_sem_preco", "unidades", "vendas_codigo_sem_cadastro"}
# Palavras dos nomes de coluna com o acento de volta ("diferenca_por_unidade" -> "diferença por unidade").
ACENTOS = {"preco": "preço", "precos": "preços", "diferenca": "diferença", "farmacia": "farmácia",
           "farmacias": "farmácias", "concentracao": "concentração", "medio": "médio", "media": "média",
           "minimo": "mínimo", "maximo": "máximo", "mes": "mês", "codigo": "código", "unitario": "unitário",
           "comparacao": "comparação", "cartao": "cartão", "ultima": "última", "ultimo": "último",
           "numero": "número", "variacao": "variação", "pix": "Pix", "ean": "EAN", "farmazids": "Farmazids"}


def separar_modo(historico):
    """(histórico sem a marca do modo, modo). A tela põe {"modo": "rapida"} no fim do histórico."""
    modo = next((t.get("modo") for t in historico if isinstance(t, dict) and "modo" in t), None)
    return [t for t in historico if not (isinstance(t, dict) and "modo" in t)], modo


def numero(valor):
    if valor is None or str(valor).strip().lower() in ("", "none", "nan", "null"):
        return None
    try:
        return float(str(valor).replace(",", "."))
    except ValueError:
        return None


def reais(valor):
    return "R$ " + f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def inteiro(valor):
    return f"{valor:,.0f}".replace(",", ".")


def em_dinheiro(coluna):
    return coluna not in CONTAGENS and "percent" not in coluna and (
        "preco" in coluna or "valor" in coluna or coluna.startswith("diferenca"))


def rotulo(coluna):
    """"menor_preco_concorrente" -> "menor preço concorrente"."""
    return " ".join(ACENTOS.get(p, p) for p in coluna.lower().split("_"))


def farmacia(valor):
    return NOMES_FARMACIAS.get(str(valor or "").lower(), str(valor or ""))


def data_br(valor):
    achado = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(valor or ""))
    return f"{achado.group(3)}/{achado.group(2)}/{achado.group(1)}" if achado else str(valor or "")


def frase_rapida(colunas, linhas):
    """1 ou 2 frases com a resposta e o número principal, montadas só com os dados."""
    if not linhas:
        return "Não encontrei dados para essa pergunta."
    nomes = [c.lower() for c in colunas]
    indice = {n: i for i, n in enumerate(nomes)}

    def valor(linha, nome):
        i = indice.get(nome)
        return linha[i] if i is not None and i < len(linha) else None

    # Vendas: total e valor estimado.
    if "total_vendas" in indice:
        total = sum(numero(valor(l, "total_vendas")) or 0 for l in linhas)
        estimado = sum(numero(valor(l, "valor_estimado")) or 0 for l in linhas)
        frase = f"Foram {inteiro(total)} {'venda' if total == 1 else 'vendas'}"
        return frase + (f", com valor estimado de {reais(estimado)} (preços atuais)." if estimado else ".")

    # Farmazids x concorrentes, o mesmo produto: a maior diferença em reais.
    if {"preco_farmazids", "menor_preco_concorrente", "diferenca"} <= set(indice):
        primeira = max(linhas, key=lambda l: abs(numero(valor(l, "diferenca")) or 0))
        dif = numero(valor(primeira, "diferenca")) or 0
        quem = farmacia(valor(primeira, "farmacia"))
        frase = (f"A maior diferença é em {valor(primeira, 'produto') or 'um produto'}: a Farmazids cobra "
                 f"{reais(numero(valor(primeira, 'preco_farmazids')))} e a {quem}, "
                 f"{reais(numero(valor(primeira, 'menor_preco_concorrente')))} ({reais(abs(dif))} "
                 f"{'a menos' if dif > 0 else 'a mais'}).")
        return frase + f" São {inteiro(len(linhas))} produtos na lista."

    # Comparação por unidade: o mais barato na apresentação vendida pelo maior número de farmácias.
    por_unidade = next((n for n in nomes if PRECO_POR_UNIDADE.search(n)), None)
    if por_unidade and "farmacia" in indice:
        grupos = {}
        for l in linhas:
            if (numero(valor(l, por_unidade)) or 0) > 0:
                chave = (valor(l, "concentracao"), valor(l, "forma"), valor(l, "unidade"))
                grupos.setdefault(chave, []).append(l)
        if grupos:
            grupo = max(grupos.values(), key=lambda g: (len({str(valor(l, "farmacia")).lower() for l in g}), len(g)))
            grupo.sort(key=lambda l: numero(valor(l, por_unidade)))
            melhor = grupo[0]
            unidade = str(valor(melhor, "unidade") or "unidade")
            produto = valor(melhor, "produto") or valor(melhor, "nome")
            frase = (f"O menor preço é na {farmacia(valor(melhor, 'farmacia'))}: "
                     f"{reais(numero(valor(melhor, por_unidade)))} por {unidade}" + (f" ({produto})." if produto else "."))
            posicao = next((i for i, l in enumerate(grupo, 1) if str(valor(l, "farmacia")).lower() == "farmazids"), None)
            if posicao and posicao > 1:
                frase += (f" A Farmazids está em {posicao}º lugar, com "
                          f"{reais(numero(valor(grupo[posicao - 1], por_unidade)))} por {unidade}.")
            return frase

    # Evolução: o menor preço na coleta mais recente.
    preco = next((n for n in nomes if em_dinheiro(n) and not PRECO_POR_UNIDADE.search(n)), None)
    if "data_coleta" in indice and preco:
        ultima = max(str(valor(l, "data_coleta")) for l in linhas)
        recentes = [l for l in linhas if str(valor(l, "data_coleta")) == ultima and numero(valor(l, preco)) is not None]
        if recentes:
            melhor = min(recentes, key=lambda l: numero(valor(l, preco)))
            onde = f", na {farmacia(valor(melhor, 'farmacia'))}" if "farmacia" in indice else ""
            return f"Na coleta de {data_br(ultima)}, o menor preço foi {reais(numero(valor(melhor, preco)))}{onde}."

    # Qualquer outro resultado: em português, o primeiro item e o número principal dele (nada de
    # "campo: valor" cru).
    def mostrar(nome, bruto):
        n = numero(bruto)
        return reais(n) if em_dinheiro(nome) else (inteiro(n) if n == int(n) else f"{n:,.2f}".replace(
            ",", "X").replace(".", ",").replace("X", "."))

    primeira = linhas[0]
    texto = next((farmacia(primeira[i]) if c == "farmacia" else str(primeira[i])
                  for i, c in enumerate(nomes) if i < len(primeira) and primeira[i] not in (None, "")
                  and numero(primeira[i]) is None and c not in ("concentracao", "forma", "unidade")), None)
    valor_principal = next(((c, primeira[i]) for i, c in enumerate(nomes)
                            if i < len(primeira) and numero(primeira[i]) is not None), None)
    numero_txt = f"{rotulo(valor_principal[0])} de {mostrar(*valor_principal)}" if valor_principal else ""
    if len(linhas) == 1:
        if texto and numero_txt:
            return f"{texto}: {numero_txt}."
        return f"O resultado é {texto or mostrar(*valor_principal)}." if (texto or valor_principal) else "Pronto."
    inicio = f"São {inteiro(len(linhas))} resultados."
    if texto:
        return f"{inicio} O primeiro é {texto}" + (f", com {numero_txt}." if numero_txt else ".")
    return inicio + (f" O primeiro tem {numero_txt}." if numero_txt else "")


def ferramenta(com_grafico, com_fala):
    """A resposta sempre; o gráfico só quando pedido; a fala só fora da comparação por unidade."""
    propriedades = {"resposta": RESPOSTA}
    if com_fala:
        propriedades["fala"] = FALA
    if com_grafico:
        propriedades["grafico"] = GRAFICO
    descricao = "Entrega a resposta para o usuário" + (" e a sugestão de gráfico." if com_grafico else ".")
    return {
        "tools": [{"toolSpec": {
            "name": "responder",
            "description": descricao,
            "inputSchema": {"json": {"type": "object", "properties": propriedades,
                                     "required": sorted(propriedades)}},
        }}],
        "toolChoice": {"tool": {"name": "responder"}},
    }


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


def validar_grafico(grafico, colunas, linhas):
    """Confere a sugestão do modelo: tipo conhecido e colunas que existem no
    resultado. Se algo não bater, não sugere gráfico (null). A tela confere de
    novo se os valores são números antes de desenhar."""
    if not isinstance(grafico, dict) or len(linhas) < 2:
        return None
    nomes = {c.lower(): c for c in colunas}

    def coluna(nome):
        return nomes.get(nome.lower()) if isinstance(nome, str) else None

    tipo, x = grafico.get("tipo"), coluna(grafico.get("x"))
    y = []
    for nome in grafico.get("y") or []:
        if coluna(nome) and coluna(nome) != x and coluna(nome) not in y:
            y.append(coluna(nome))
    if tipo not in TIPOS_GRAFICO or not x or not y:
        return None
    y = y[:MAXIMO_VALORES]
    serie = coluna(grafico.get("serie"))
    if len(y) > 1 or serie in (x, *y):
        serie = None  # várias colunas de valores já viram várias séries
    return {"tipo": tipo, "x": x, "y": y, "serie": serie, "pedido": True}  # só há gráfico quando pedido


def tempo(etapa, inicio):
    """Log para medir: "[tempo] etapa=bedrock 1.23 s"."""
    print(f"[tempo] etapa={etapa} {time.perf_counter() - inicio:.2f} s")


def lambda_handler(event, context):
    inicio = time.perf_counter()
    try:
        return redigir(event)
    finally:
        tempo("total", inicio)


def redigir(event):
    colunas, linhas = event.get("colunas", []), event.get("linhas", [])
    historico, modo = separar_modo(event.get("historico") or [])
    if modo == "rapida":
        # Sem Bedrock: a frase sai dos dados. A tela mostra a tabela em "Ver detalhes".
        print("[tempo] etapa=rapida (sem Bedrock)")
        return {"resposta": frase_rapida(colunas, linhas), "grafico": None, "fala": None}
    rotulo = "Pergunta atual" if historico else "Pergunta"
    pediu_grafico = bool(PEDIU_GRAFICO.search(event["pergunta"]))
    com_fala = not any(PRECO_POR_UNIDADE.search(c) for c in colunas)
    mensagem = (
        f"{texto_historico(historico)}"
        f"{rotulo}: {event['pergunta']}\n\n"
        f"Resultado ({len(linhas)} linhas, colunas separadas por |):\n"
        f"{tabela_em_texto(colunas, linhas)}"
    )
    inicio = time.perf_counter()
    resp = bedrock.converse(
        modelId=MODEL_ID,
        system=[{"text": SYSTEM + (INSTRUCOES_FALA if com_fala else "") + (INSTRUCOES_GRAFICO if pediu_grafico else "")}],
        messages=[{"role": "user", "content": [{"text": mensagem}]}],
        inferenceConfig={"maxTokens": MAX_TOKENS},
        toolConfig=ferramenta(pediu_grafico, com_fala),
    )
    tempo("bedrock", inicio)
    uso = resp.get("usage", {})
    print(f"[tokens] entrada={uso.get('inputTokens')} saida={uso.get('outputTokens')} linhas={len(linhas)}")
    conteudo = resp["output"]["message"]["content"]
    entrada = next((b["toolUse"]["input"] for b in conteudo if "toolUse" in b), {})
    # Se o modelo responder em texto, sem a ferramenta, o texto vira a resposta, sem gráfico.
    texto = (entrada.get("resposta") or next((b["text"] for b in conteudo if "text" in b), "")).strip()
    if not texto:
        raise ValueError("O Bedrock não devolveu resposta.")  # a máquina tenta de novo (Retry)
    grafico = validar_grafico(entrada.get("grafico"), colunas, linhas) if pediu_grafico else None
    fala = (entrada.get("fala") or "").strip() if com_fala else ""
    return {"resposta": texto, "grafico": grafico, "fala": fala or None}
