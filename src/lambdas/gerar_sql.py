"""Agente 1 - Gerar SQL.

Recebe:  {"pergunta": "...", "erro_anterior": "", "historico": [...]}
         erro_anterior vem vazio na 1ª vez. historico é opcional: lista de
         {"pergunta", "resposta", "sql"} das últimas trocas, da mais antiga para
         a mais recente. Sem histórico, funciona como antes.
Devolve: {"sql": "..."}
Se a pergunta for só teórica (ex.: "para que serve a dipirona?"), devolve
{"sql": "PERGUNTA_TEORICA"} e a máquina manda para o agente Responder teórica.
Se a pergunta não puder ser respondida com as tabelas, devolve {"sql": "CANNOT_ANSWER"}.
"""
import os
import re
import unicodedata

import boto3
from botocore.config import Config

REGION = os.environ.get("AWS_REGION", "us-east-2")
GLUE_DATABASE = os.environ.get("GLUE_DATABASE", "t1_eq1_farmazids")
MODEL_ID = os.environ.get("MODEL_ID", "us.anthropic.claude-haiku-5-5")
# Mesmas tabelas que o agente Validar SQL aceita; o modelo só enxerga essas.
ALLOWED_TABLES = {
    t.strip().lower()
    for t in os.environ.get(
        "ALLOWED_TABLES",
        "vw_medicamentos,vw_precos_concorrentes,vw_vendas,vw_precos_unitarios,vw_precos_unitarios_farmazids",
    ).split(",")
    if t.strip()
}
# A tela já manda no máximo 3 trocas; o corte aqui é só uma garantia.
TROCAS_NO_HISTORICO = 3

_retry = Config(retries={"max_attempts": 4, "mode": "adaptive"})
glue = boto3.client("glue", region_name=REGION, config=_retry)
bedrock = boto3.client("bedrock-runtime", region_name=REGION, config=_retry)

# Conhecimento sobre os dados que o Glue não tem.
DICAS_DADOS = (
    "- vw_precos_concorrentes: preços coletados nos sites das farmácias concorrentes "
    "(preco_sem_desconto, preco_cartao, preco_pix, em_estoque), um registro por produto e coleta. "
    "data_coleta é um timestamp (ex.: 2026-10-11 15:47:43); para 'preço atual', use a coleta mais recente "
    "de cada farmácia e produto. categorias é um array: use contains(categorias, '...') ou UNNEST.\n"
    "- vw_vendas: vendas (uma linha por venda) com só estas colunas: id_venda, farmacia, ean_gtin (código "
    "de barras do produto vendido), data_venda e quantidade_vendida. Não tem preço, valor, nome do produto "
    "nem SKU: o produto e o preço vêm do código de barras (ver a regra de vendas). Só tem vendas de "
    "farmazids, farmaponte e veracruz.\n"
    "- vw_medicamentos: catálogo da Farmazids com o preço próprio (preco_farmazids).\n"
    "- vw_precos_unitarios: as mesmas linhas da vw_precos_concorrentes, mais o que foi lido do nome do "
    "produto: quantidade (da embalagem), unidade ('comprimido', 'capsula', 'ml', 'g', 'dose', 'sache'...), "
    "concentracao (ex.: '500mg', '1000mg' para 1g, '500mg/ml', '50mg+12.5mg' para combinações), forma "
    "('comprimido', 'comprimido efervescente', 'comprimido liberacao prolongada', 'capsula', 'gotas', "
    "'solucao', 'xarope', 'creme'...) e preco_pix_por_unidade e preco_cartao_por_unidade. Para comparar "
    "farmácias, tem também preco_comparacao (o preço Pix; quando ele está vazio, o do cartão: a São João "
    "não informa Pix), preco_comparacao_origem ('pix' ou 'cartao') e preco_comparacao_por_unidade. Quando o "
    "nome não diz a quantidade, quantidade e o preço por unidade são nulos.\n"
    "- vw_precos_unitarios_farmazids: a vw_medicamentos com as mesmas colunas lidas do nome e "
    "preco_farmazids_por_unidade. Não tem farmacia, data de coleta nem estoque (é a tabela de preços).\n"
    "- As views se ligam pela coluna ean (código de barras do produto).\n"
    "- A coluna farmacia pode vir escrita de formas diferentes em cada view. Filtre sempre com LOWER e "
    "aceitando as variações: Farmaponte -> LOWER(farmacia) = 'farmaponte'; VeraCruz -> LOWER(farmacia) = "
    "'veracruz'; Farmazids -> LOWER(farmacia) = 'farmazids'; São Paulo (Drogaria São Paulo) -> "
    "LOWER(farmacia) IN ('sao_paulo', 'são paulo', 'sao paulo'); São João -> LOWER(farmacia) IN "
    "('são joão', 'sao_joao', 'sao joao').\n"
    "- Quando o produto está fora de estoque, a São Paulo grava preco_sem_desconto = 9999876. Esse valor "
    "não é um preço: exclua-o de médias, mínimos, máximos e comparações (preco_sem_desconto <> 9999876)."
)

# Comparação justa: pelo preço por unidade, só entre produtos equivalentes.
REGRA_COMPARACAO = (
    "- REGRA FIRME: toda pergunta sobre o preço de um remédio ('preço', 'quanto custa', 'mais barato', "
    "'mais caro', 'onde comprar', 'onde encontrar', 'compare', 'qual compensa', 'quem vende mais barato', "
    "'valor') usa vw_precos_unitarios (e vw_precos_unitarios_farmazids quando a Farmazids entrar), nunca "
    "vw_precos_concorrentes, e segue o modelo abaixo. Vale também com o texto sem acento, sem pontuação, "
    "em minúsculas ou vindo de reconhecimento de voz (ex.: 'onde o paracetamol 750 e mais barato'), com uma "
    "farmácia só ('quanto custa a dipirona na farmaponte': filtre a farmácia) e com a concentração "
    "incompleta: número sem unidade depois do remédio é a concentração em mg ('paracetamol 750' -> "
    "LOWER(u.nome) LIKE '%paracetamol%' AND u.concentracao LIKE '750mg%'; nunca ponha o número no LIKE do "
    "nome). A única exceção é evolução ao longo do tempo, que segue a regra de evolução (também na "
    "vw_precos_unitarios, com o preço de comparação).\n"
    "- Na comparação, o preço é sempre o preço de comparação (preco_comparacao e "
    "preco_comparacao_por_unidade), nunca preco_pix sozinho: sem ele, a São João fica de fora. Filtros de "
    "preço ('abaixo de R$ 10') e a ordem também usam esse preço. Traga sempre preco_comparacao_origem AS "
    "origem_preco (a tela marca o preço do cartão).\n"
    "- No modelo, compare só dentro do remédio "
    "pedido (filtro pelo nome; se a pergunta citar concentração ou forma, filtre também). Agrupe por "
    "concentracao, forma, unidade e farmacia: produtos com concentração, forma ou unidade diferentes não são "
    "equivalentes. Traga o nome do produto, a marca, o estoque e a data da coleta com min_by (a tela monta "
    "os cartões com essas colunas, com estes nomes) e ordene pelo preço por unidade. A marca é sempre a do "
    "produto mais barato (min_by, sem reserva): se ele não tiver marca, ela fica vazia, nunca a de outro "
    "produto. Produtos sem "
    "quantidade podem aparecer, com o preço por unidade vazio. Use a coleta mais recente de cada farmácia. "
    "Modelo (troque 'dipirona' pelo remédio pedido; tire o UNION ALL se a Farmazids não entrar):\n"
    "WITH ultima AS (SELECT farmacia, max(date(data_coleta)) AS dia FROM vw_precos_unitarios GROUP BY farmacia), "
    "precos AS (SELECT u.farmacia, u.nome, CAST(u.marca AS varchar) AS marca, u.concentracao, u.forma, "
    "u.unidade, u.quantidade, u.preco_comparacao AS preco, "
    "u.preco_comparacao_por_unidade AS preco_por_unidade, u.preco_comparacao_origem AS origem_preco, "
    "CAST(u.em_estoque AS varchar) AS em_estoque, "
    "date(u.data_coleta) AS data_coleta "
    "FROM vw_precos_unitarios u JOIN ultima ON ultima.farmacia = u.farmacia AND date(u.data_coleta) = ultima.dia "
    "WHERE LOWER(u.nome) LIKE '%dipirona%' "
    "UNION ALL SELECT 'farmazids', nome, CAST(NULL AS varchar), concentracao, forma, unidade, quantidade, "
    "TRY_CAST(preco_farmazids AS double), preco_farmazids_por_unidade, CAST(NULL AS varchar), "
    "CAST(NULL AS varchar), CAST(NULL AS date) "
    "FROM vw_precos_unitarios_farmazids WHERE LOWER(nome) LIKE '%dipirona%') "
    "SELECT farmacia, COALESCE(min_by(nome, preco_por_unidade), min(nome)) AS produto, "
    "min_by(marca, preco_por_unidade) AS marca, "
    "min(preco_por_unidade) AS preco_por_unidade, "
    "COALESCE(min_by(preco, preco_por_unidade), min(preco)) AS preco, "
    "COALESCE(min_by(origem_preco, preco_por_unidade), min(origem_preco)) AS origem_preco, "
    "COALESCE(min_by(quantidade, preco_por_unidade), min(quantidade)) AS quantidade, "
    "concentracao, forma, unidade, "
    "COALESCE(min_by(em_estoque, preco_por_unidade), max(em_estoque)) AS em_estoque, "
    "max(data_coleta) AS data_coleta "
    "FROM precos GROUP BY concentracao, forma, unidade, farmacia "
    "ORDER BY concentracao, forma, unidade, preco_por_unidade NULLS LAST LIMIT 50\n"
    "- Se a pergunta for sobre um produto específico da Farmazids comparado aos concorrentes, ligue "
    "vw_precos_unitarios_farmazids e vw_precos_unitarios pelo ean (mesmo produto e embalagem).\n"
    "- Código de barras (EAN: 8 ou 13 dígitos, ex.: 'produto com código de barras 7891234567895'): use o "
    "mesmo modelo, com a Farmazids, mas troque o filtro pelo nome pelo filtro do ean nas duas partes: "
    "TRIM(CAST(u.ean AS varchar)) = '7891234567895' na vw_precos_unitarios e TRIM(CAST(ean AS varchar)) = "
    "'7891234567895' na vw_precos_unitarios_farmazids. Esse número é o código, não a concentração: não "
    "filtre concentração nem nome.\n"
    "- Perguntas que não são de preço (lista de produtos, estoque, catálogo) continuam com as views de "
    "sempre. Perguntas de vendas seguem a regra de vendas.\n"
)

# Vendas: a base não tem preço nem nome; os dois vêm do código de barras (ean_gtin).
REGRA_VENDAS = (
    "- Vendas (quantidade vendida, vendas por mês ou farmácia, valor, faturamento): use o modelo abaixo. "
    "O produto e o preço vêm do código de barras, nesta ordem: a coleta mais recente da mesma farmácia; "
    "senão, a mediana da coleta mais recente das outras farmácias; senão, a vw_medicamentos (Farmazids). "
    "Compare os códigos sem zeros à esquerda. O preço é o de comparação (preco_comparacao: Pix; se vazio, "
    "cartão). Traga SEMPRE total_vendas, vendas_com_preco, vendas_sem_preco e valor_estimado (soma de "
    "quantidade x preço, só das vendas com preço), com estes nomes. Nunca chame o valor de 'arrecadado' "
    "nem os produtos de 'SKU'. Com filtro de remédio (ex.: 'dipirona', como no exemplo), as vendas com "
    "código sem cadastro (nome IS NULL) ficam FORA de total_vendas e do valor, porque não são comprovadamente "
    "do remédio; elas vêm à parte, na coluna vendas_codigo_sem_cadastro (todas as vendas da farmácia e do "
    "mês com código sem cadastro). Troque os filtros do exemplo pelos da pergunta (farmácia, período, "
    "agrupamento):\n"
    "WITH vendas AS (SELECT id_venda, farmacia, "
    "regexp_replace(regexp_replace(translate(lower(trim(farmacia)), 'áàâãéêíóôõúç', 'aaaaeeiooouc'), "
    "'[^a-z]', ''), '^drogaria', '') AS farmacia_n, "
    "regexp_replace(trim(CAST(ean_gtin AS varchar)), '^0+', '') AS ean, "
    "TRY_CAST(quantidade_vendida AS double) AS quantidade, date(data_venda) AS data_venda "
    "FROM vw_vendas WHERE date(data_venda) BETWEEN DATE '2026-01-01' AND DATE '2026-01-31' "
    "AND LOWER(farmacia) = 'farmaponte'), "
    "coleta AS (SELECT regexp_replace(regexp_replace(translate(lower(trim(farmacia)), 'áàâãéêíóôõúç', "
    "'aaaaeeiooouc'), '[^a-z]', ''), '^drogaria', '') AS farmacia_n, "
    "regexp_replace(trim(CAST(ean AS varchar)), '^0+', '') AS ean, max_by(nome, data_coleta) AS nome, "
    "max_by(preco_comparacao, data_coleta) AS preco FROM vw_precos_unitarios "
    "WHERE ean IS NOT NULL AND preco_comparacao IS NOT NULL GROUP BY 1, 2), "
    "outras AS (SELECT ean, min(nome) AS nome, approx_percentile(preco, 0.5) AS preco FROM coleta GROUP BY ean), "
    "farmazids AS (SELECT regexp_replace(trim(CAST(ean AS varchar)), '^0+', '') AS ean, min(nome) AS nome, "
    "min(TRY_CAST(preco_farmazids AS double)) AS preco FROM vw_medicamentos WHERE ean IS NOT NULL GROUP BY 1), "
    "identificadas AS (SELECT v.*, "
    "CASE WHEN c.preco IS NOT NULL THEN c.nome WHEN o.preco IS NOT NULL THEN o.nome ELSE f.nome END AS nome, "
    "COALESCE(c.preco, o.preco, f.preco) AS preco FROM vendas v "
    "LEFT JOIN coleta c ON c.farmacia_n = v.farmacia_n AND c.ean = v.ean "
    "LEFT JOIN outras o ON o.ean = v.ean LEFT JOIN farmazids f ON f.ean = v.ean) "
    "SELECT farmacia, date_format(data_venda, '%Y-%m') AS mes, "
    "count_if(LOWER(nome) LIKE '%dipirona%') AS total_vendas, "
    "count_if(LOWER(nome) LIKE '%dipirona%' AND preco IS NOT NULL) AS vendas_com_preco, "
    "count_if(LOWER(nome) LIKE '%dipirona%' AND preco IS NULL) AS vendas_sem_preco, "
    "sum(CASE WHEN LOWER(nome) LIKE '%dipirona%' THEN quantidade END) AS unidades, "
    "round(sum(CASE WHEN LOWER(nome) LIKE '%dipirona%' THEN quantidade * preco END), 2) AS valor_estimado, "
    "count_if(nome IS NULL) AS vendas_codigo_sem_cadastro "
    "FROM identificadas GROUP BY 1, 2 "
    "HAVING count_if(LOWER(nome) LIKE '%dipirona%') > 0 OR count_if(nome IS NULL) > 0 "
    "ORDER BY 1, 2 LIMIT 50\n"
    "- A vw_vendas só tem Farmazids, FarmaPonte e VeraCruz. Se a pergunta de vendas (ou a continuação de "
    "uma, como 'e na São João?') for só de outra farmácia (São João, Drogaria São Paulo), responda "
    "exatamente CANNOT_ANSWER. Se citar farmácias de dentro e de fora, filtre só as de dentro.\n"
    "- Sem filtro de remédio (vendas totais), as vendas com código sem cadastro entram no total: "
    "count(*) AS total_vendas, count_if(preco IS NOT NULL) AS vendas_com_preco, count_if(preco IS NULL) AS "
    "vendas_sem_preco, sum(quantidade) AS unidades, round(sum(quantidade * preco), 2) AS valor_estimado, "
    "sem HAVING e sem a coluna vendas_codigo_sem_cadastro. Para listar produtos vendidos, agrupe por nome e "
    "traga o código como ean_gtin (o nome fica vazio quando o código não tem cadastro).\n"
)

# Palavras de pergunta de preço, já sem acento e em minúsculas (ver eh_pergunta_de_preco).
PERGUNTA_DE_PRECO = re.compile(
    r"\b(preco|precos|custa|custam|custo|barat\w*|car[oa]s?|compar\w*|compensa|valor|valores"
    r"|onde (comprar|compro|encontr\w*|acho|achar|vende\w*))\b"
)
# Vendas seguem a regra de vendas, não o modelo de comparação ("valor" das vendas não é preço).
PERGUNTA_DE_VENDAS = re.compile(r"\b(vend\w*|faturamento|faturou|faturad\w*|arrecad\w*|receita)\b")
# Farmácias da base de vendas (vw_vendas) e as que não estão nela (só têm preços coletados),
# no texto já sem acento. A tela usa a mesma lista para explicar.
FARMACIAS_COM_VENDAS = {
    "Farmazids": re.compile(r"\bfarmazids\b"),
    "FarmaPonte": re.compile(r"\bfarma\s?ponte\b"),
    "VeraCruz": re.compile(r"\bvera\s?cruz\b"),
}
FARMACIAS_SEM_VENDAS = {
    "São João": re.compile(r"\bsao\s?joao\b"),
    "Drogaria São Paulo": re.compile(r"\b(?:drogaria\s+)?sao\s?paulo\b"),
}
LEMBRETE_SO_COM_VENDAS = (
    "\n\n(Lembrete: a vw_vendas não tem {fora}. Filtre só as farmácias que ela tem: {dentro}.)"
)
LEMBRETE_VENDAS = (
    "\n\n(Lembrete: é uma pergunta de vendas. Siga a regra de vendas: preço pelo código de barras, "
    "sempre com total_vendas, vendas_com_preco, vendas_sem_preco e valor_estimado.)"
)
# Evolução ao longo do tempo segue a regra de evolução, não o modelo de comparação.
PERGUNTA_DE_EVOLUCAO = re.compile(
    r"\b(evolu\w*|historico|ao longo|variou|variacao|subiu|caiu|grafico|tendencia|por (dia|mes|semana))\b"
)
# Código de barras na pergunta (o leitor da tela manda "... com código de barras <EAN>").
EAN_NA_PERGUNTA = re.compile(r"(?<!\d)(\d{13}|\d{8})(?!\d)")
LEMBRETE_EAN = (
    "\n\n(Lembrete: {ean} é o código de barras (EAN) do produto. Filtre pelo ean nas duas partes do "
    "modelo de comparação, não pelo nome nem pela concentração.)"
)
LEMBRETE_PRECO = (
    "\n\n(Lembrete: é uma pergunta de preço de remédio. Siga a REGRA FIRME: vw_precos_unitarios e o "
    "modelo de comparação, com as mesmas colunas.)"
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
        "- Sempre termine com LIMIT (no máximo 50).\n"
        "- Quando a tabela tiver partições, filtre por elas sempre que a pergunta permitir.\n"
        "- Evolução de preço ao longo do tempo (ou gráfico de evolução): use vw_precos_unitarios e o preço de "
        "comparação (preco_comparacao: o Pix; se vazio, o cartão), nunca preco_pix sozinho (sem ele, a São "
        "João fica de fora). Traga uma linha por dia de coleta e farmácia, com date(data_coleta) AS "
        "data_coleta, ordenada pela data, e a origem do preço como origem_preco (a tela marca o cartão). Se "
        "a pergunta citar um produto específico (apresentação ou marca, ex.: 'dipirona 500mg 10 comprimidos'), "
        "filtre só esse produto e traga preco_comparacao AS preco e preco_comparacao_origem AS origem_preco. "
        "Se for genérica (ex.: 'dipirona'), traga MIN(preco_comparacao) AS menor_preco, o menor preço entre "
        "os produtos encontrados em cada coleta, e min_by(preco_comparacao_origem, preco_comparacao) AS "
        "origem_preco.\n"
        f"{REGRA_COMPARACAO}"
        f"{REGRA_VENDAS}"
        "- Responda APENAS com o SQL, sem explicações e sem blocos de código.\n"
        "- Se vier uma conversa anterior e a pergunta atual for uma continuação (ex.: 'e na São João?', "
        "'e o mais barato?'), reaproveite produto, filtros e colunas do SQL anterior e mude só o que a "
        "pergunta pedir. Se for um assunto novo, ignore a conversa anterior.\n"
        "- Se a pergunta for só teórica sobre remédios ou saúde (para que serve, o que é, como age, efeitos, "
        "diferença entre genérico, similar e referência, se dois remédios são a mesma coisa) e não pedir "
        "nenhum dado, responda exatamente PERGUNTA_TEORICA.\n"
        "- É pergunta de dados (gere o SQL) tudo que depende das views: preços, estoque, vendas, catálogo "
        "('quais remédios temos?', 'qual o remédio mais vendido?') e comparação com os concorrentes. Se a "
        "pergunta misturar teoria e dados (ex.: 'para que serve a dipirona e quanto custa?'), gere o SQL "
        "da parte de dados.\n"
        "- Se a pergunta não for teórica e não puder ser respondida com essas views (ex.: assuntos fora de "
        "remédios e saúde), responda exatamente CANNOT_ANSWER.\n\n"
        f"Sobre os dados:\n{DICAS_DADOS}"
    )


def limpar_sql(texto):
    texto = re.sub(r"```(sql)?", "", texto, flags=re.IGNORECASE)
    return texto.strip().rstrip(";").strip()


def texto_historico(historico):
    """Conversa anterior para pôr antes da pergunta. Leva a pergunta e o SQL de
    cada troca (a resposta não ajuda a montar o SQL). Vazio se não houver histórico."""
    if not historico:
        return ""
    linhas = ["Conversa anterior (da mais antiga para a mais recente):"]
    for numero, troca in enumerate(historico[-TROCAS_NO_HISTORICO:], start=1):
        linhas.append(f"{numero}. Pergunta: {troca.get('pergunta', '')}")
        if troca.get("sql"):
            linhas.append(f"   SQL usado: {troca['sql']}")
    return "\n".join(linhas) + "\n\nPergunta atual: "


def sem_acento(texto):
    return "".join(c for c in unicodedata.normalize("NFD", texto.lower()) if unicodedata.category(c) != "Mn")


def eh_pergunta_de_preco(pergunta):
    """Pergunta de preço de remédio que não é de evolução. O lembrete vai junto da pergunta,
    para o modelo não voltar à vw_precos_concorrentes (com ou sem acento e pontuação)."""
    texto = sem_acento(pergunta)
    return (bool(PERGUNTA_DE_PRECO.search(texto)) and not PERGUNTA_DE_EVOLUCAO.search(texto)
            and not eh_pergunta_de_vendas(pergunta))


def eh_pergunta_de_vendas(pergunta):
    return bool(PERGUNTA_DE_VENDAS.search(sem_acento(pergunta)))


def farmacias_citadas(pergunta, farmacias):
    texto = sem_acento(pergunta)
    return [nome for nome, padrao in farmacias.items() if padrao.search(texto)]


def lambda_handler(event, context):
    pergunta = event["pergunta"]
    erro_anterior = event.get("erro_anterior") or ""
    historico = event.get("historico") or []

    if eh_pergunta_de_vendas(pergunta):
        fora = farmacias_citadas(pergunta, FARMACIAS_SEM_VENDAS)
        dentro = farmacias_citadas(pergunta, FARMACIAS_COM_VENDAS)
        if fora and not dentro:
            # Vendas só de farmácias fora da base: nem Bedrock nem Athena (a tela explica).
            return {"sql": "CANNOT_ANSWER"}

    mensagem = texto_historico(historico) + pergunta
    if eh_pergunta_de_vendas(pergunta):
        mensagem += LEMBRETE_VENDAS
        if fora:
            mensagem += LEMBRETE_SO_COM_VENDAS.format(fora=" nem ".join(fora), dentro=", ".join(dentro))
    elif eh_pergunta_de_preco(pergunta):
        mensagem += LEMBRETE_PRECO
    ean = EAN_NA_PERGUNTA.search(pergunta)
    if ean:
        mensagem += LEMBRETE_EAN.format(ean=ean.group(1))
    if erro_anterior:
        mensagem += f"\n\nA tentativa anterior foi reprovada:\n{erro_anterior}\n\nCorrija o SQL."

    resp = bedrock.converse(
        modelId=MODEL_ID,
        system=[{"text": system_prompt()}],
        messages=[{"role": "user", "content": [{"text": mensagem}]}],
        # Sem temperature nem top_p: este modelo recusa os dois ("deprecated"). A regra firme
        # e o lembrete de preço é que mantêm o SQL estável.
        inferenceConfig={"maxTokens": 4000},
    )
    texto = next(b["text"] for b in resp["output"]["message"]["content"] if "text" in b)
    sql = limpar_sql(texto)
    if "PERGUNTA_TEORICA" in sql:
        sql = "PERGUNTA_TEORICA"
    elif "CANNOT_ANSWER" in sql:
        sql = "CANNOT_ANSWER"
    return {"sql": sql}
