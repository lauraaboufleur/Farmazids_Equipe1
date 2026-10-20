"""Agente 4 - Redigir resposta.

Recebe:  {"pergunta": "...", "colunas": ["..."], "linhas": [[...], ...], "historico": [...]}
         historico é opcional: lista de {"pergunta", "resposta", "sql"} das últimas
         trocas, da mais antiga para a mais recente. Aqui só entram a pergunta e a
         resposta resumida. Sem histórico, funciona como antes.
Devolve: {"resposta": "...", "grafico": {...} ou null}
         grafico é a sugestão de gráfico para a tela:
         {"tipo": "barras" | "linha", "x": coluna, "y": [colunas de valores],
          "serie": coluna que separa as linhas (ou null), "pedido": true}.
         Só há gráfico quando a pergunta pede ("mostra em gráfico", "faz um
         gráfico"...). Sem pedido, o modelo nem recebe as instruções de gráfico
         (economiza tokens) e grafico vem null. A chave vem sempre.
"""
import os
import re

import boto3
from botocore.config import Config

REGION = os.environ.get("AWS_REGION", "us-east-2")
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-haiku-5-5")
LINHAS_NO_PROMPT = int(os.environ.get("LINHAS_NO_PROMPT", "100"))
# A tela já manda no máximo 3 trocas; o corte aqui é só uma garantia.
TROCAS_NO_HISTORICO = 3
TIPOS_GRAFICO = {"barras", "linha"}
MAXIMO_VALORES = 3  # quantas colunas de valores um gráfico pode ter
PEDIU_GRAFICO = re.compile(r"gr[áa]fico", re.IGNORECASE)  # a tela confere com a mesma regra

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
    "sobre o seu caso, procure um farmacêutico ou médico.'\n"
    "Comparação por unidade: se o resultado tiver preço por unidade (colunas como preco_por_unidade ou "
    "preco_pix_por_unidade), a tela já mostra o ranking, as outras apresentações e a tabela. Então a "
    "resposta é UMA frase só, sem listas, sem marcadores, sem quebras de linha, sem markdown e sem "
    "títulos: quem é mais barato por unidade na apresentação (concentração e forma) vendida pelo maior "
    "número de farmácias, ou na que a pergunta pediu, com o preço por unidade. Exemplo: 'O paracetamol "
    "750 mg sai mais barato na FarmaPonte, a R$ 0,34 por comprimido.' Ignore os remédios combinados "
    "(concentração com '+', ex.: '500mg+30mg'), a menos que a pergunta cite a combinação. Preço por "
    "unidade com 2 casas decimais. Nada além dessa frase.\n\n"
    "Entregue sempre pela ferramenta responder."
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


def ferramenta(com_grafico):
    """Sem pedido de gráfico, a ferramenta só tem a resposta."""
    propriedades = {"resposta": RESPOSTA, "grafico": GRAFICO} if com_grafico else {"resposta": RESPOSTA}
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


def lambda_handler(event, context):
    colunas, linhas = event.get("colunas", []), event.get("linhas", [])
    historico = event.get("historico") or []
    rotulo = "Pergunta atual" if historico else "Pergunta"
    pediu_grafico = bool(PEDIU_GRAFICO.search(event["pergunta"]))
    mensagem = (
        f"{texto_historico(historico)}"
        f"{rotulo}: {event['pergunta']}\n\n"
        f"Resultado ({len(linhas)} linhas, colunas separadas por |):\n"
        f"{tabela_em_texto(colunas, linhas)}"
    )
    resp = bedrock.converse(
        modelId=MODEL_ID,
        system=[{"text": SYSTEM + (INSTRUCOES_GRAFICO if pediu_grafico else "")}],
        messages=[{"role": "user", "content": [{"text": mensagem}]}],
        inferenceConfig={"maxTokens": 4000},
        toolConfig=ferramenta(pediu_grafico),
    )
    conteudo = resp["output"]["message"]["content"]
    entrada = next((b["toolUse"]["input"] for b in conteudo if "toolUse" in b), {})
    # Se o modelo responder em texto, sem a ferramenta, o texto vira a resposta, sem gráfico.
    texto = (entrada.get("resposta") or next((b["text"] for b in conteudo if "text" in b), "")).strip()
    if not texto:
        raise ValueError("O Bedrock não devolveu resposta.")  # a máquina tenta de novo (Retry)
    grafico = validar_grafico(entrada.get("grafico"), colunas, linhas) if pediu_grafico else None
    return {"resposta": texto, "grafico": grafico}
