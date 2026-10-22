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
import time
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
# O SQL mais longo (o de vendas) tem uns 700 tokens: 1500 sobra, e corta uma resposta desembestada.
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "1500"))
# Prompt caching do Bedrock: o system prompt (regras + esquema) é igual em toda chamada, então fica
# em cache (cachePoint) e as chamadas seguintes leem só a pergunta. "0" desliga.
CACHE_PROMPT = os.environ.get("CACHE_PROMPT", "1") == "1"

_retry = Config(retries={"max_attempts": 4, "mode": "adaptive"})
glue = boto3.client("glue", region_name=REGION, config=_retry)
bedrock = boto3.client("bedrock-runtime", region_name=REGION, config=_retry)

# Código de barras sem zeros à esquerda: é assim que as views se ligam.
def ean_sem_zeros(coluna):
    return f"regexp_replace(trim(CAST({coluna} AS varchar)), '^0+', '')"


# Conhecimento sobre os dados que o Glue não tem.
DICAS_DADOS = (
    "- vw_precos_unitarios: preços dos concorrentes só na coleta mais recente de cada farmácia (sempre "
    "atualizada; a view já filtra). Além das colunas da coleta, tem o que foi lido do nome: quantidade, unidade "
    "('comprimido', 'capsula', 'ml', 'g'...), concentracao ('500mg', '1000mg' para 1g, '500mg/ml', "
    "'50mg+12.5mg'), forma ('comprimido', 'comprimido efervescente', 'comprimido liberacao prolongada', "
    "'gotas', 'solucao', 'xarope'...), e o preço de comparação: preco_comparacao (Pix; se vazio, cartão: a "
    "São João não tem Pix), preco_comparacao_origem ('pix' ou 'cartao') e preco_comparacao_por_unidade "
    "(nulo quando o nome não diz a quantidade).\n"
    "- vw_precos_concorrentes: todas as coletas (o histórico), sem as colunas lidas do nome: só para evolução "
    "ao longo do tempo. data_coleta é timestamp; categorias é array (contains ou UNNEST).\n"
    "- vw_precos_unitarios_farmazids: a tabela de preços da Farmazids (ean, nome, preco_farmazids, as colunas "
    "lidas do nome e preco_farmazids_por_unidade), sem farmácia, data nem estoque. vw_medicamentos: o mesmo "
    "catálogo, sem as colunas lidas do nome.\n"
    "- vw_vendas: uma linha por venda (id_venda, farmacia, ean_gtin, data_venda, quantidade_vendida), sem "
    "preço, nome nem SKU (ver a regra de vendas). Só tem Farmazids, FarmaPonte e VeraCruz.\n"
    f"- As views se ligam pelo código de barras sem zeros à esquerda: {ean_sem_zeros('ean')}.\n"
    "- farmacia: filtre com LOWER e as variações: 'farmaponte'; 'veracruz'; 'farmazids'; Drogaria São Paulo "
    "-> IN ('sao_paulo', 'são paulo', 'sao paulo'); São João -> IN ('são joão', 'sao_joao', 'sao joao').\n"
    "- 9999876 (São Paulo fora de estoque) não é preço: exclua de médias, mínimos e comparações."
)


def sql_comparacao(filtro_atual, filtro_farmazids):
    """Modelo da comparação por unidade: concorrentes (vw_precos_unitarios) + Farmazids, agrupados por
    concentração, forma e unidade. As colunas e os nomes são os que a tela usa para os cartões."""
    return (
        "WITH precos AS (SELECT farmacia, nome, CAST(marca AS varchar) AS marca, concentracao, forma, unidade, "
        "quantidade, preco_comparacao AS preco, preco_comparacao_por_unidade AS preco_por_unidade, "
        "preco_comparacao_origem AS origem_preco, CAST(em_estoque AS varchar) AS em_estoque, "
        f"date(data_coleta) AS data_coleta FROM vw_precos_unitarios WHERE {filtro_atual} "
        "UNION ALL SELECT 'farmazids', nome, CAST(NULL AS varchar), concentracao, forma, unidade, quantidade, "
        "TRY_CAST(preco_farmazids AS double), preco_farmazids_por_unidade, CAST(NULL AS varchar), "
        f"CAST(NULL AS varchar), CAST(NULL AS date) FROM vw_precos_unitarios_farmazids WHERE {filtro_farmazids}) "
        "SELECT farmacia, COALESCE(min_by(nome, preco_por_unidade), min(nome)) AS produto, "
        "min_by(marca, preco_por_unidade) AS marca, min(preco_por_unidade) AS preco_por_unidade, "
        "COALESCE(min_by(preco, preco_por_unidade), min(preco)) AS preco, "
        "COALESCE(min_by(origem_preco, preco_por_unidade), min(origem_preco)) AS origem_preco, "
        "COALESCE(min_by(quantidade, preco_por_unidade), min(quantidade)) AS quantidade, concentracao, forma, "
        "unidade, COALESCE(min_by(em_estoque, preco_por_unidade), max(em_estoque)) AS em_estoque, "
        "max(data_coleta) AS data_coleta FROM precos GROUP BY concentracao, forma, unidade, farmacia "
        "ORDER BY concentracao, forma, unidade, preco_por_unidade NULLS LAST LIMIT 50"
    )


def sql_comparacao_ean(ean):
    """A comparação por código de barras (a pergunta do leitor da tela), montada sem o Bedrock."""
    filtro = f" = '{ean.lstrip('0')}'"
    return sql_comparacao(ean_sem_zeros("ean") + filtro, ean_sem_zeros("ean") + filtro)


def sql_farmazids_x_concorrentes(mais_cara=True):
    """Farmazids x concorrentes, o MESMO produto (mesmo EAN): produto, preço Farmazids, menor preço
    concorrente (preço de comparação), farmácia e diferença em R$ (sem percentual)."""
    filtro, ordem = ("f.preco_farmazids > m.menor_preco_concorrente", "diferenca DESC") if mais_cara \
        else ("f.preco_farmazids < m.menor_preco_concorrente", "diferenca ASC")
    return (
        f"WITH menor AS (SELECT {ean_sem_zeros('ean')} AS ean_n, min(preco_comparacao) AS menor_preco_concorrente, "
        "min_by(farmacia, preco_comparacao) AS farmacia FROM vw_precos_unitarios "
        "WHERE ean IS NOT NULL AND preco_comparacao IS NOT NULL GROUP BY 1), "
        f"farmazids AS (SELECT {ean_sem_zeros('ean')} AS ean_n, min(nome) AS produto, "
        "min(TRY_CAST(preco_farmazids AS double)) AS preco_farmazids FROM vw_precos_unitarios_farmazids "
        "WHERE ean IS NOT NULL GROUP BY 1) "
        "SELECT f.produto, f.preco_farmazids, m.menor_preco_concorrente, m.farmacia, "
        "round(f.preco_farmazids - m.menor_preco_concorrente, 2) AS diferenca "
        f"FROM farmazids f JOIN menor m ON m.ean_n = f.ean_n WHERE {filtro} ORDER BY {ordem} LIMIT 20"
    )


def sql_vendas_modelo():
    """Modelo de vendas: o produto e o preço vêm do código de barras (coleta da mesma farmácia; senão,
    mediana das outras; senão, Farmazids). Exemplo com filtro de remédio (dipirona)."""
    farmacia_n = "lower(regexp_replace(farmacia, '[^A-Za-z]', ''))"
    return (
        f"WITH vendas AS (SELECT farmacia, {farmacia_n} AS farmacia_n, {ean_sem_zeros('ean_gtin')} AS ean, "
        "TRY_CAST(quantidade_vendida AS double) AS quantidade, date(data_venda) AS data_venda FROM vw_vendas "
        "WHERE date(data_venda) BETWEEN DATE '2026-01-01' AND DATE '2026-01-31' AND LOWER(farmacia) = 'farmaponte'), "
        f"coleta AS (SELECT {farmacia_n} AS farmacia_n, {ean_sem_zeros('ean')} AS ean, min(nome) AS nome, "
        "min(preco_comparacao) AS preco FROM vw_precos_unitarios WHERE ean IS NOT NULL AND preco_comparacao IS NOT NULL "
        "GROUP BY 1, 2), "
        "outras AS (SELECT ean, min(nome) AS nome, approx_percentile(preco, 0.5) AS preco FROM coleta GROUP BY ean), "
        f"farmazids AS (SELECT {ean_sem_zeros('ean')} AS ean, min(nome) AS nome, "
        "min(TRY_CAST(preco_farmazids AS double)) AS preco FROM vw_medicamentos WHERE ean IS NOT NULL GROUP BY 1), "
        "identificadas AS (SELECT v.*, CASE WHEN c.preco IS NOT NULL THEN c.nome WHEN o.preco IS NOT NULL THEN o.nome "
        "ELSE f.nome END AS nome, COALESCE(c.preco, o.preco, f.preco) AS preco FROM vendas v "
        "LEFT JOIN coleta c ON c.farmacia_n = v.farmacia_n AND c.ean = v.ean LEFT JOIN outras o ON o.ean = v.ean "
        "LEFT JOIN farmazids f ON f.ean = v.ean) "
        "SELECT farmacia, date_format(data_venda, '%Y-%m') AS mes, "
        "count_if(LOWER(nome) LIKE '%dipirona%') AS total_vendas, "
        "count_if(LOWER(nome) LIKE '%dipirona%' AND preco IS NOT NULL) AS vendas_com_preco, "
        "count_if(LOWER(nome) LIKE '%dipirona%' AND preco IS NULL) AS vendas_sem_preco, "
        "sum(CASE WHEN LOWER(nome) LIKE '%dipirona%' THEN quantidade END) AS unidades, "
        "round(sum(CASE WHEN LOWER(nome) LIKE '%dipirona%' THEN quantidade * preco END), 2) AS valor_estimado, "
        "count_if(nome IS NULL) AS vendas_codigo_sem_cadastro FROM identificadas GROUP BY 1, 2 "
        "HAVING count_if(LOWER(nome) LIKE '%dipirona%') > 0 OR count_if(nome IS NULL) > 0 ORDER BY 1, 2 LIMIT 50"
    )


REGRA_EVOLUCAO = (
    "- Evolução de preço ao longo do tempo (ou gráfico de evolução): vw_precos_concorrentes (tem o histórico), "
    "com o preço de comparação COALESCE(NULLIF(TRY_CAST(preco_pix AS double), 0), "
    "NULLIF(TRY_CAST(preco_cartao AS double), 0)) (exclua 9999876) e CASE WHEN TRY_CAST(preco_pix AS double) > 0 "
    "THEN 'pix' ELSE 'cartao' END AS origem_preco. Uma linha por dia e farmácia, date(data_coleta) AS "
    "data_coleta, ordenada pela data. Produto específico ('dipirona 500mg 10 comprimidos'): filtre-o e traga o "
    "preço AS preco. Genérico ('dipirona'): MIN do preço AS menor_preco e min_by(origem, preço) AS origem_preco.\n"
)

REGRA_COMPARACAO = (
    "- Preço de remédio ('preço', 'quanto custa', 'mais barato/caro', 'onde comprar', 'compare', 'qual "
    "compensa', 'valor'), também sem acento, sem pontuação ou vindo de voz: use SEMPRE o modelo abaixo, "
    "nunca vw_precos_concorrentes. Uma farmácia só: filtre a farmácia. Número sem unidade depois do remédio "
    "é a concentração em mg ('paracetamol 750' -> LOWER(nome) LIKE '%paracetamol%' AND concentracao LIKE "
    "'750mg%'; nunca o número no LIKE do nome). Concentração ou forma citadas: filtre também. Só são "
    "equivalentes os produtos com a mesma concentracao, forma e unidade (o agrupamento do modelo). A marca é a "
    "do produto mais barato (min_by, sem reserva). Mantenha as colunas e os nomes do modelo (a tela usa). Sem "
    "a Farmazids na pergunta, tire o UNION ALL. Modelo para 'dipirona':\n"
    + sql_comparacao("LOWER(nome) LIKE '%dipirona%'", "LOWER(nome) LIKE '%dipirona%'") + "\n"
    "- Código de barras (EAN, 8 ou 13 dígitos): o mesmo modelo, filtrando as duas partes por "
    f"{ean_sem_zeros('ean')} = '<código sem zeros à esquerda>' em vez do nome; o número não é concentração.\n"
    "- Perguntas que não são de preço nem de vendas (lista de produtos, estoque, catálogo): as views de sempre.\n"
)

REGRA_VENDAS = (
    "- Vendas (quantidade vendida, por mês ou farmácia, valor, faturamento): use o modelo abaixo. O produto e o "
    "preço vêm do código de barras: coleta da mesma farmácia; senão, mediana das outras; senão, vw_medicamentos. "
    "Traga SEMPRE total_vendas, vendas_com_preco, vendas_sem_preco e valor_estimado (quantidade x preço, só das "
    "vendas com preço), com estes nomes. Nunca 'arrecadado' nem 'SKU'. Com filtro de remédio (como no modelo), "
    "as vendas com código sem cadastro (nome IS NULL) ficam FORA do total e do valor e vêm à parte em "
    "vendas_codigo_sem_cadastro. Sem filtro de remédio, elas entram no total: count(*) AS total_vendas, "
    "count_if(preco IS NOT NULL) AS vendas_com_preco, count_if(preco IS NULL) AS vendas_sem_preco, "
    "sum(quantidade) AS unidades, round(sum(quantidade * preco), 2) AS valor_estimado, sem HAVING e sem "
    "vendas_codigo_sem_cadastro. Para listar produtos vendidos, agrupe por nome e traga ean_gtin. A vw_vendas "
    "só tem Farmazids, FarmaPonte e VeraCruz: vendas só de outra farmácia (inclusive numa continuação, como "
    "'e na São João?') -> CANNOT_ANSWER; farmácias de dentro e de fora -> filtre só as de dentro. Troque os "
    "filtros do modelo pelos da pergunta:\n" + sql_vendas_modelo() + "\n"
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
    "\n\n(Lembrete: é uma pergunta de preço de remédio. Use o modelo de comparação (vw_precos_unitarios), com "
    "as mesmas colunas.)"
)

# A pergunta do atalho "Onde a Farmazids está mais cara" tem o SQL montado aqui, sem o Bedrock.
PERGUNTA_FARMAZIDS_MAIS_CARA = re.compile(
    r"^em quais produtos a farmazids esta mais cara (?:que|do que) os concorrentes\s*\??$")

# "Farmazids mais cara/barata que os concorrentes" (texto sem acento): lembrete da regra abaixo.
PERGUNTA_FARMAZIDS_X = re.compile(
    r"farmazids.*\bmais (?:cara|caro|barata|barato)s?\b.*\b(?:concorrent\w*|outras farmacias|farmacias)\b")
LEMBRETE_FARMAZIDS_X = (
    "\n\n(Lembrete: é a comparação Farmazids x concorrentes produto a produto. Siga a regra dela: mesmo "
    "ean, colunas produto, preco_farmazids, menor_preco_concorrente, farmacia e diferenca, sem percentual.)"
)
REGRA_FARMAZIDS_X_CONCORRENTES = (
    "- Farmazids mais cara (ou mais barata) que os concorrentes: só o MESMO produto (mesmo ean), nunca "
    "produtos diferentes, sem percentual. Colunas: produto, preco_farmazids, menor_preco_concorrente, farmacia, "
    "diferenca (R$); maior diferença primeiro, LIMIT 20. Modelo (mais cara; mais barata: troque > por < e "
    "ordene ASC):\n" + sql_farmazids_x_concorrentes() + "\n"
)

# A pergunta que o leitor de código de barras da tela manda (texto já sem acento).
PERGUNTA_DO_LEITOR = re.compile(r"^compare os precos do produto com codigo de barras (\d{8}|\d{13})\s*\??$")


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
        f"{REGRA_EVOLUCAO}"
        f"{REGRA_COMPARACAO}"
        f"{REGRA_FARMAZIDS_X_CONCORRENTES}"
        f"{REGRA_VENDAS}"
        "- Responda APENAS com o SQL, sem explicações e sem blocos de código.\n"
        "- Continuação da conversa anterior ('e na São João?', 'e o mais barato?'): reaproveite produto, "
        "filtros e colunas do SQL anterior e mude só o que a pergunta pedir; assunto novo: ignore a conversa.\n"
        "- Só teórica, sem pedir dado (para que serve, como age, efeitos, genérico x similar x referência): "
        "responda exatamente PERGUNTA_TEORICA. Teoria e dados juntos: gere o SQL da parte de dados.\n"
        "- É de dados tudo que depende das views (preços, estoque, vendas, catálogo, concorrentes). Fora de "
        "remédios e saúde, ou sem como responder com elas: responda exatamente CANNOT_ANSWER.\n\n"
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


def tempo(etapa, inicio):
    """Log para medir: "[tempo] etapa=bedrock 1.23 s" (CloudWatch Logs Insights filtra por "[tempo]")."""
    print(f"[tempo] etapa={etapa} {time.perf_counter() - inicio:.2f} s")


def chamar_bedrock(mensagem):
    """Converse com o system prompt em cache (cachePoint). Se o modelo ou a conta não aceitarem o cache,
    tenta de novo sem ele e não usa mais nesta Lambda."""
    global CACHE_PROMPT
    inicio = time.perf_counter()
    prompt = system_prompt()
    tempo("prompt", inicio)  # inclui ler o esquema do Glue na primeira chamada
    inicio = time.perf_counter()
    system = [{"text": prompt}] + ([{"cachePoint": {"type": "default"}}] if CACHE_PROMPT else [])
    pedido = dict(modelId=MODEL_ID, messages=[{"role": "user", "content": [{"text": mensagem}]}],
                  # Sem temperature nem top_p: este modelo recusa os dois ("deprecated").
                  inferenceConfig={"maxTokens": MAX_TOKENS})
    try:
        resp = bedrock.converse(system=system, **pedido)
    except bedrock.exceptions.ValidationException as erro:
        if not CACHE_PROMPT or "cach" not in str(erro).lower():
            raise
        print(f"[chatbot] prompt caching recusado, seguindo sem: {erro}")
        CACHE_PROMPT = False
        resp = bedrock.converse(system=[{"text": prompt}], **pedido)
    tempo("bedrock", inicio)
    uso = resp.get("usage", {})
    print(f"[tokens] entrada={uso.get('inputTokens')} cache_lido={uso.get('cacheReadInputTokens')} "
          f"cache_gravado={uso.get('cacheWriteInputTokens')} saida={uso.get('outputTokens')}")
    return resp


def lambda_handler(event, context):
    inicio = time.perf_counter()
    try:
        return gerar(event)
    finally:
        tempo("total", inicio)


def gerar(event):
    pergunta = event["pergunta"]
    erro_anterior = event.get("erro_anterior") or ""
    # A tela pode mandar {"modo": "rapida"} no fim do histórico (para a redigir-resposta): não é uma troca.
    historico = [t for t in (event.get("historico") or []) if not (isinstance(t, dict) and "modo" in t)]

    if PERGUNTA_FARMAZIDS_MAIS_CARA.match(sem_acento(pergunta).strip()) and not erro_anterior:
        # Pergunta do atalho "Onde a Farmazids está mais cara": o SQL sai pronto, sem Bedrock.
        return {"sql": sql_farmazids_x_concorrentes()}

    leitor = PERGUNTA_DO_LEITOR.match(sem_acento(pergunta).strip())
    if leitor and not erro_anterior:
        # Pergunta do leitor de código de barras: o SQL sai pronto, sem Bedrock (mais rápido e barato).
        # Se ele for reprovado (erro_anterior), a correção volta para o caminho normal.
        return {"sql": sql_comparacao_ean(leitor.group(1))}

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
    elif PERGUNTA_FARMAZIDS_X.search(sem_acento(pergunta)):
        mensagem += LEMBRETE_FARMAZIDS_X
    elif eh_pergunta_de_preco(pergunta):
        mensagem += LEMBRETE_PRECO
    ean = EAN_NA_PERGUNTA.search(pergunta)
    if ean:
        mensagem += LEMBRETE_EAN.format(ean=ean.group(1))
    if erro_anterior:
        mensagem += f"\n\nA tentativa anterior foi reprovada:\n{erro_anterior}\n\nCorrija o SQL."

    resp = chamar_bedrock(mensagem)
    texto = next(b["text"] for b in resp["output"]["message"]["content"] if "text" in b)
    sql = limpar_sql(texto)
    if "PERGUNTA_TEORICA" in sql:
        sql = "PERGUNTA_TEORICA"
    elif "CANNOT_ANSWER" in sql:
        sql = "CANNOT_ANSWER"
    return {"sql": sql}
