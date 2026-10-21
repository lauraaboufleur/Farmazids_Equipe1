"""Dados do dashboard da Farmazids.

Aqui ficam as consultas no Athena e as contas; nada de tela (a tela é o dashboard.py).
O dashboard não passa pelo Bedrock: roda SQL fixo direto no Athena, poucas consultas
grandes de uma vez, e o resto é filtrado no pandas. Os dados mudam 1 vez por dia
(as tabelas novas chegam e a vw_precos_unitarios é recriada às 13:30), então o
dashboard.py guarda o resultado até a próxima atualização.

No modo simulado (CHATBOT_MODO_SIMULADO, o mesmo do chatbot), os dados são inventados
aqui mesmo, sem consultar a AWS.
"""

import io
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

REGIAO = os.environ.get("AWS_REGION", "us-east-2")
BANCO = os.environ.get("GLUE_DATABASE", "t1_eq1_farmazids")
WORKGROUP = os.environ.get("ATHENA_WORKGROUP", "primary")
PASTA_RESULTADOS = os.environ.get("DASHBOARD_ATHENA_OUTPUT", "s3://farmazids-t1-equipe1/athena-results/dashboard/")
# O usuário das chaves do Streamlit (treinamento1-leitor-dados) não pode usar o Athena, e as
# permissões dele não podem ser mudadas. Então o dashboard "veste" esta role, que confia nesse
# usuário e tem as permissões do iam-policy-dashboard.json. Vazio = usa as chaves direto.
ROLE_DASHBOARD = os.environ.get("DASHBOARD_ROLE_ARN", "arn:aws:iam::906513713169:role/t1-eq1-dashboard-leitura")

SEM_ESTOQUE = 9999876  # "preço" que a São Paulo grava quando está fora de estoque
FARMACIAS_COM_VENDAS = ["farmazids", "farmaponte", "veracruz"]  # a base de vendas só tem essas
CONCORRENTES = ["farmaponte", "veracruz", "sao_joao", "sao_paulo"]
# A mesma farmácia escrita de jeitos diferentes em cada view vira uma chave só.
CHAVES_FARMACIAS = {
    "farmaponte": "farmaponte", "farma ponte": "farmaponte", "veracruz": "veracruz", "vera cruz": "veracruz",
    "drogaria veracruz": "veracruz", "sao_joao": "sao_joao", "são joão": "sao_joao", "sao joao": "sao_joao",
    "sao_paulo": "sao_paulo", "são paulo": "sao_paulo", "sao paulo": "sao_paulo",
    "drogaria são paulo": "sao_paulo", "drogaria sao paulo": "sao_paulo", "farmazids": "farmazids",
}
# Abaixo disso de diferença no preço, conta como empate (centavos de arredondamento).
EMPATE = 0.005
# Desconto mínimo (sobre o preço sem desconto) para o produto contar como "em promoção".
DESCONTO_PROMOCAO = 0.05

# ---------- Consultas ----------
# Como na gerar-sql: na vw_vendas o código de barras é ean_gtin e a quantidade pode vir como texto.
DATA_VENDA = "date(data_venda)"
QUANTIDADE = "TRY_CAST(quantidade_vendida AS double)"

CONSULTAS = {
    # Unidades vendidas por farmácia, produto e mês.
    "vendas": f"""
        SELECT lower(farmacia) AS farmacia, CAST(ean_gtin AS varchar) AS ean,
               CAST(date_trunc('month', {DATA_VENDA}) AS varchar) AS mes,
               sum({QUANTIDADE}) AS unidades, count(*) AS vendas
        FROM vw_vendas
        GROUP BY 1, 2, 3""",
    # Unidades por farmácia, ano e dia da semana (1 = segunda ... 7 = domingo).
    "semana": f"""
        SELECT lower(farmacia) AS farmacia, year(d) AS ano, day_of_week(d) AS dia_semana,
               sum(q) AS unidades
        FROM (SELECT farmacia, {QUANTIDADE} AS q, {DATA_VENDA} AS d FROM vw_vendas)
        GROUP BY 1, 2, 3""",
    "ultima_venda": f"SELECT CAST(max({DATA_VENDA}) AS varchar) AS ultima FROM vw_vendas",
    # Preço mais recente de cada farmácia e produto.
    "precos": """
        SELECT farmacia, ean, nome, marca, categorias, preco_sem_desconto, preco_cartao, preco_pix,
               em_estoque, data_coleta
        FROM (
            SELECT lower(farmacia) AS farmacia, CAST(ean AS varchar) AS ean, nome, marca,
                   array_join(categorias, '|') AS categorias,
                   preco_sem_desconto, preco_cartao, preco_pix,
                   CAST(em_estoque AS varchar) AS em_estoque, CAST(data_coleta AS varchar) AS data_coleta,
                   row_number() OVER (PARTITION BY lower(farmacia), CAST(ean AS varchar)
                                      ORDER BY data_coleta DESC) AS n
            FROM vw_precos_concorrentes)
        WHERE n = 1""",
    "medicamentos": "SELECT CAST(ean AS varchar) AS ean, nome, preco_farmazids FROM vw_medicamentos",
}

# Histórico de preço de um produto (aba Consulta de produto): um ponto por farmácia e dia.
CONSULTA_HISTORICO = """
    SELECT lower(farmacia) AS farmacia, CAST(date(data_coleta) AS varchar) AS dia,
           min(preco_pix) AS preco_pix, min(preco_cartao) AS preco_cartao,
           min(CASE WHEN preco_sem_desconto <> 9999876 THEN preco_sem_desconto END) AS preco_sem_desconto
    FROM vw_precos_concorrentes
    WHERE regexp_replace(CAST(ean AS varchar), '^0+', '') = '{ean}'
    GROUP BY 1, 2"""

NUMERICAS = {"unidades", "vendas", "ano", "dia_semana", "preco_sem_desconto", "preco_cartao", "preco_pix",
             "preco_farmazids"}


def clientes():
    """Clientes do Athena e do S3, com as permissões da role do dashboard (credenciais
    temporárias de 1 hora, pedidas de novo a cada carga)."""
    import boto3  # só no modo real

    if not ROLE_DASHBOARD:
        return boto3.client("athena", region_name=REGIAO), boto3.client("s3", region_name=REGIAO)
    chaves = boto3.client("sts", region_name=REGIAO).assume_role(
        RoleArn=ROLE_DASHBOARD, RoleSessionName="dashboard-streamlit")["Credentials"]
    sessao = boto3.Session(aws_access_key_id=chaves["AccessKeyId"], aws_secret_access_key=chaves["SecretAccessKey"],
                           aws_session_token=chaves["SessionToken"], region_name=REGIAO)
    return sessao.client("athena"), sessao.client("s3")


def rodar(sql, athena=None, s3=None):
    """Roda uma consulta no Athena e devolve um DataFrame (tudo como texto, menos as colunas
    de NUMERICAS). Lê o CSV do resultado direto do S3: bem mais rápido que GetQueryResults
    de 1.000 em 1.000 linhas."""
    if athena is None or s3 is None:
        athena, s3 = clientes()
    id_consulta = athena.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": BANCO},
        ResultConfiguration={"OutputLocation": PASTA_RESULTADOS},
        WorkGroup=WORKGROUP,
    )["QueryExecutionId"]
    while True:
        execucao = athena.get_query_execution(QueryExecutionId=id_consulta)["QueryExecution"]
        estado = execucao["Status"]["State"]
        if estado == "SUCCEEDED":
            break
        if estado in ("FAILED", "CANCELLED"):
            raise RuntimeError(f"Athena falhou: {execucao['Status'].get('StateChangeReason')}")
        time.sleep(0.4)
    local = execucao["ResultConfiguration"]["OutputLocation"]  # s3://bucket/pasta/id.csv
    bucket, chave = local.removeprefix("s3://").split("/", 1)
    corpo = s3.get_object(Bucket=bucket, Key=chave)["Body"].read()
    tabela = pd.read_csv(io.BytesIO(corpo), dtype=str, keep_default_na=False, na_values=[""])
    for coluna in tabela.columns:
        if coluna in NUMERICAS:
            tabela[coluna] = pd.to_numeric(tabela[coluna], errors="coerce")
    return tabela


def carregar_tudo():
    """As consultas da base, todas ao mesmo tempo (cada uma leva alguns segundos)."""
    athena, s3 = clientes()
    with ThreadPoolExecutor(max_workers=len(CONSULTAS)) as executor:
        tarefas = {nome: executor.submit(rodar, sql, athena, s3) for nome, sql in CONSULTAS.items()}
        return {nome: tarefa.result() for nome, tarefa in tarefas.items()}


def carregar_historico(ean):
    ean = chave_ean(ean)
    if not ean:  # só dígitos entram no SQL
        return pd.DataFrame(columns=["farmacia", "dia", "preco_pix", "preco_cartao", "preco_sem_desconto"])
    return rodar(CONSULTA_HISTORICO.format(ean=ean))


# ---------- Arrumação ----------
def chave_ean(valor):
    """O mesmo código de barras pode vir com ou sem zeros à esquerda: só os dígitos, sem eles."""
    digitos = re.sub(r"\D", "", str(valor or ""))
    return digitos.lstrip("0") or None


def chave_farmacia(valor):
    texto = str(valor or "").strip().lower()
    return CHAVES_FARMACIAS.get(texto, texto.replace(" ", "_"))


def preparar(base):
    """Deixa as tabelas da base prontas para as contas: farmácia e EAN padronizados, o 9999876
    fora dos preços, o preço de comparação (Pix; se vazio, cartão) e o nome e as categorias de
    cada produto. Devolve um dicionário com as tabelas prontas."""
    vendas = base["vendas"].copy()
    vendas["farmacia"] = vendas["farmacia"].map(chave_farmacia)
    vendas["ean"] = vendas["ean"].map(chave_ean)
    vendas["mes"] = pd.to_datetime(vendas["mes"].str[:10], errors="coerce")
    vendas = vendas.dropna(subset=["mes"])
    vendas["ano"] = vendas["mes"].dt.year

    semana = base["semana"].copy()
    semana["farmacia"] = semana["farmacia"].map(chave_farmacia)

    precos = base["precos"].copy()
    precos["farmacia"] = precos["farmacia"].map(chave_farmacia)
    precos["ean"] = precos["ean"].map(chave_ean)
    for coluna in ("preco_sem_desconto", "preco_cartao", "preco_pix"):
        precos[coluna] = pd.to_numeric(precos[coluna], errors="coerce")
    placeholder = (precos[["preco_sem_desconto", "preco_cartao", "preco_pix"]] == SEM_ESTOQUE).any(axis=1)
    precos[["preco_sem_desconto", "preco_cartao", "preco_pix"]] = (
        precos[["preco_sem_desconto", "preco_cartao", "preco_pix"]].where(lambda p: (p != SEM_ESTOQUE) & (p > 0)))
    estoque = precos["em_estoque"].astype(str).str.lower()
    precos["sem_estoque"] = (estoque == "false") | placeholder
    precos["preco"] = precos["preco_pix"].fillna(precos["preco_cartao"]).fillna(precos["preco_sem_desconto"])
    precos["no_cartao"] = precos["preco_pix"].isna() & precos["preco_cartao"].notna()
    precos["coleta"] = pd.to_datetime(precos["data_coleta"].str[:19], errors="coerce")
    menor = precos[["preco_pix", "preco_cartao"]].min(axis=1)
    precos["desconto"] = ((precos["preco_sem_desconto"] - menor) / precos["preco_sem_desconto"]).clip(lower=0)
    precos = precos.dropna(subset=["ean"])

    medicamentos = base["medicamentos"].copy()
    medicamentos["ean"] = medicamentos["ean"].map(chave_ean)
    medicamentos["preco_farmazids"] = pd.to_numeric(medicamentos["preco_farmazids"], errors="coerce")
    medicamentos = medicamentos.dropna(subset=["ean"]).drop_duplicates("ean")

    # A Farmazids entra na tabela de preços como mais uma farmácia (preço do catálogo).
    farmazids = pd.DataFrame({
        "farmacia": "farmazids", "ean": medicamentos["ean"], "nome": medicamentos["nome"],
        "preco": medicamentos["preco_farmazids"], "sem_estoque": False, "no_cartao": False,
    })
    precos_todas = pd.concat([precos[precos["farmacia"] != "farmazids"], farmazids], ignore_index=True)

    # Nome: o do catálogo da Farmazids; se não tiver, o mais comum nos sites.
    nomes = medicamentos.set_index("ean")["nome"]
    nomes_sites = precos.dropna(subset=["nome"]).groupby("ean")["nome"].agg(lambda n: n.mode().iat[0])
    produtos = pd.DataFrame(index=pd.Index(sorted(set(nomes.index) | set(nomes_sites.index)
                                               | set(vendas["ean"].dropna())), name="ean"))
    produtos["nome"] = nomes.reindex(produtos.index).fillna(nomes_sites.reindex(produtos.index))
    produtos["nome"] = produtos["nome"].fillna("Código " + produtos.index.to_series())
    categorias = (precos.dropna(subset=["categorias"]).groupby("ean")["categorias"]
                  .agg(lambda c: sorted({p.strip() for texto in c for p in str(texto).split("|") if p.strip()})))
    produtos["categorias"] = categorias.reindex(produtos.index)
    produtos["categorias"] = produtos["categorias"].map(lambda c: c if isinstance(c, list) else [])
    produtos["no_catalogo"] = produtos.index.isin(medicamentos["ean"])
    produtos = produtos.reset_index()

    ultima = pd.to_datetime((base["ultima_venda"]["ultima"].iat[0] if len(base["ultima_venda"]) else None),
                            errors="coerce")
    return {"vendas": vendas, "semana": semana, "precos": precos_todas, "produtos": produtos,
            "ultima_venda": ultima, "ultima_coleta": precos["coleta"].max()}


def filtrar_categorias(dados, categorias):
    """Só os produtos de alguma das categorias escolhidas (nenhuma escolhida = todos)."""
    if not categorias:
        return dados
    escolhidas = set(categorias)
    eans = set(dados["produtos"].loc[dados["produtos"]["categorias"].map(lambda c: bool(escolhidas & set(c))), "ean"])
    return {**dados,
            "vendas": dados["vendas"][dados["vendas"]["ean"].isin(eans)],
            "precos": dados["precos"][dados["precos"]["ean"].isin(eans)],
            "produtos": dados["produtos"][dados["produtos"]["ean"].isin(eans)]}


def todas_categorias(produtos):
    return sorted({c for lista in produtos["categorias"] for c in lista})


# ---------- Receita (estimada) ----------
def receita(dados):
    """Vendas com valor estimado (a base de vendas não tem o preço de cada venda): unidades ×
    preço atual, com a mesma regra da gerar-sql, para os números baterem com os do chat.
    Farmazids: o preço do catálogo dela (ela não tem site coletado). Outras farmácias: a coleta
    mais recente da mesma farmácia; senão, a mediana das farmácias coletadas; senão, o preço do
    catálogo da Farmazids. Venda de código sem preço fica com valor vazio."""
    precos = dados["precos"].dropna(subset=["preco"])
    coletados = precos[precos["farmacia"] != "farmazids"]
    mesma = coletados.drop_duplicates(["farmacia", "ean"]).set_index(["farmacia", "ean"])["preco"]
    mediana = coletados.groupby("ean")["preco"].median()
    catalogo = precos[precos["farmacia"] == "farmazids"].drop_duplicates("ean").set_index("ean")["preco"]
    vendas = dados["vendas"].copy()
    chaves = pd.MultiIndex.from_arrays([vendas["farmacia"], vendas["ean"]])
    outras = (pd.Series(mesma.reindex(chaves).to_numpy(), index=vendas.index)
              .fillna(vendas["ean"].map(mediana)).fillna(vendas["ean"].map(catalogo)))
    vendas["preco"] = outras.where(vendas["farmacia"] != "farmazids",
                                   vendas["ean"].map(catalogo).fillna(outras))
    vendas["valor"] = vendas["unidades"] * vendas["preco"]
    return vendas


def periodo_comparavel(vendas, ano, ultima_venda):
    """Meses do ano escolhido e do anterior que dá para comparar: se o ano está pela metade,
    o anterior entra só até o mesmo mês."""
    ultimo_mes = 12
    if ultima_venda is not None and not pd.isna(ultima_venda) and ultima_venda.year == ano:
        ultimo_mes = ultima_venda.month
    atual = vendas[(vendas["ano"] == ano) & (vendas["mes"].dt.month <= ultimo_mes)]
    anterior = vendas[(vendas["ano"] == ano - 1) & (vendas["mes"].dt.month <= ultimo_mes)]
    return atual, anterior, ultimo_mes


def variacao(atual, anterior):
    return None if not anterior else (atual - anterior) / anterior


def resumo_geral(vendas_valor, ano, ultima_venda):
    """Números dos cartões da Visão geral."""
    atual, anterior, ultimo_mes = periodo_comparavel(vendas_valor, ano, ultima_venda)
    por_farmacia = atual.groupby("farmacia")["valor"].sum()
    total = por_farmacia.sum()
    fz = atual[atual["farmacia"] == "farmazids"]
    fz_ant = anterior[anterior["farmacia"] == "farmazids"]
    ranking = por_farmacia.sort_values(ascending=False)
    return {
        "receita": fz["valor"].sum(),
        "receita_var": variacao(fz["valor"].sum(), fz_ant["valor"].sum()),
        "unidades": fz["unidades"].sum(),
        "unidades_var": variacao(fz["unidades"].sum(), fz_ant["unidades"].sum()),
        "participacao": por_farmacia.get("farmazids", 0) / total if total else None,
        "posicao": (list(ranking.index).index("farmazids") + 1) if "farmazids" in ranking.index else None,
        "farmacias": len(ranking),
        "ultimo_mes": ultimo_mes,
        "sem_preco": atual.loc[atual["preco"].isna(), "unidades"].sum() / atual["unidades"].sum()
        if atual["unidades"].sum() else 0,
    }


# ---------- Vendas ----------
def meses_para_comparar(vendas, ultima_venda):
    """Último mês completo e o anterior a ele (o mês da última venda pode estar pela metade)."""
    meses = sorted(vendas["mes"].dropna().unique())
    if ultima_venda is not None and not pd.isna(ultima_venda) and meses:
        fim_do_mes = (pd.Timestamp(ultima_venda) + pd.offsets.MonthEnd(0)).normalize()
        if pd.Timestamp(ultima_venda).normalize() < fim_do_mes:
            meses = [m for m in meses if m < pd.Timestamp(ultima_venda.year, ultima_venda.month, 1)]
    return (pd.Timestamp(meses[-2]), pd.Timestamp(meses[-1])) if len(meses) >= 2 else (None, None)


def variacao_mensal(vendas, produtos, ultima_venda, farmacia="farmazids", minimo=5):
    """Variação de cada produto da farmácia do penúltimo para o último mês completo (só os que
    venderam pelo menos `minimo` unidades em algum dos dois). Devolve (tabela, mês anterior, último mês)."""
    anterior, ultimo = meses_para_comparar(vendas, ultima_venda)
    if ultimo is None:
        return pd.DataFrame(), None, None
    da_farmacia = vendas[vendas["farmacia"] == farmacia]
    tabela = (da_farmacia[da_farmacia["mes"].isin([anterior, ultimo])]
              .pivot_table(index="ean", columns="mes", values="unidades", aggfunc="sum", fill_value=0)
              .reindex(columns=[anterior, ultimo], fill_value=0))
    tabela.columns = ["antes", "depois"]
    tabela = tabela[tabela[["antes", "depois"]].max(axis=1) >= minimo]
    tabela["variacao"] = (tabela["depois"] - tabela["antes"]) / tabela["antes"].replace(0, np.nan)
    tabela = tabela.sort_values("variacao").reset_index()
    tabela = tabela.merge(produtos[["ean", "nome"]], on="ean", how="left")
    return tabela, anterior, ultimo


# ---------- Oportunidades ----------
def comparacao_de_vendas(vendas_valor, precos, produtos, ano):
    """Para cada produto vendido no ano: unidades da Farmazids e média dos concorrentes da
    base de vendas (FarmaPonte e VeraCruz), mais os preços de hoje."""
    do_ano = vendas_valor[vendas_valor["ano"] == ano]
    unidades = do_ano.pivot_table(index="ean", columns="farmacia", values="unidades", aggfunc="sum", fill_value=0)
    unidades = unidades.reindex(columns=FARMACIAS_COM_VENDAS, fill_value=0)
    outras = [f for f in FARMACIAS_COM_VENDAS if f != "farmazids"]
    unidades["media_concorrentes"] = unidades[outras].mean(axis=1)
    unidades = unidades.reset_index().merge(produtos[["ean", "nome", "no_catalogo"]], on="ean", how="left")
    return unidades.merge(posicao_de_preco(precos), on="ean", how="left")


def posicao_de_preco(precos):
    """Por produto: preço da Farmazids, o menor preço dos concorrentes e onde ele está."""
    fz = precos[(precos["farmacia"] == "farmazids")].dropna(subset=["preco"]).set_index("ean")["preco"]
    conc = precos[(precos["farmacia"] != "farmazids")].dropna(subset=["preco"])
    if conc.empty:
        menor = pd.DataFrame(columns=["menor_concorrente", "onde"])
    else:
        idx = conc.groupby("ean")["preco"].idxmin()
        menor = conc.loc[idx].set_index("ean")[["preco", "farmacia"]]
        menor.columns = ["menor_concorrente", "onde"]
    tabela = pd.DataFrame({"preco_farmazids": fz}).join(menor, how="outer")
    tabela["diferenca"] = tabela["preco_farmazids"] - tabela["menor_concorrente"]
    tabela["diferenca_pct"] = tabela["diferenca"] / tabela["menor_concorrente"]
    tabela.index.name = "ean"
    return tabela.reset_index()


def vendendo_pouco(comparacao, limite=0.5, minimo=10):
    """Produtos em que a Farmazids vende menos que `limite` da média dos concorrentes (e os
    concorrentes vendem pelo menos `minimo` unidades). Ordenados pela receita que falta,
    estimada pelo preço da Farmazids."""
    tabela = comparacao[comparacao["no_catalogo"].fillna(False).astype(bool)
                        & (comparacao["media_concorrentes"] >= minimo)
                        & (comparacao["farmazids"] < limite * comparacao["media_concorrentes"])].copy()
    tabela["proporcao"] = tabela["farmazids"] / tabela["media_concorrentes"]
    tabela["unidades_a_mais"] = tabela["media_concorrentes"] - tabela["farmazids"]
    tabela["receita_potencial"] = tabela["unidades_a_mais"] * tabela["preco_farmazids"]
    tabela["mais_cara"] = tabela["diferenca"] > EMPATE
    return tabela.sort_values(["receita_potencial", "unidades_a_mais"], ascending=False, na_position="last")


def fora_do_catalogo(comparacao):
    """Produtos que os concorrentes vendem e a Farmazids não tem no catálogo."""
    tabela = comparacao[(~comparacao["no_catalogo"].fillna(False).astype(bool))
                        & (comparacao["media_concorrentes"] > 0)]
    return tabela.sort_values("media_concorrentes", ascending=False)


def concorrente_sem_estoque(precos, comparacao):
    """Produtos do catálogo da Farmazids que algum concorrente está sem estoque agora."""
    sem = precos[(precos["farmacia"] != "farmazids") & precos["sem_estoque"]]
    if sem.empty:
        return pd.DataFrame(columns=["ean", "farmacias_sem_estoque"])
    por_produto = sem.groupby("ean")["farmacia"].agg(lambda f: sorted(set(f))).rename("farmacias_sem_estoque")
    tabela = comparacao.merge(por_produto.reset_index(), on="ean", how="inner")
    tabela = tabela[tabela["no_catalogo"].fillna(False).astype(bool)]
    return tabela.sort_values("farmazids", ascending=False)


# ---------- Preços ----------
def comparar_precos(precos):
    """Uma linha por produto e farmácia com preço, mais o preço da Farmazids do mesmo produto."""
    fz = precos[precos["farmacia"] == "farmazids"].dropna(subset=["preco"]).set_index("ean")["preco"]
    conc = precos[precos["farmacia"] != "farmazids"].dropna(subset=["preco"]).copy()
    conc["preco_farmazids"] = conc["ean"].map(fz)
    conc = conc.dropna(subset=["preco_farmazids"])
    conc["diferenca_pct"] = conc["preco"] / conc["preco_farmazids"] - 1
    return conc


def indice_de_preco(comparados):
    """Mediana da diferença do concorrente para a Farmazids, nos produtos que os dois vendem.
    Positivo: o concorrente cobra mais que a Farmazids."""
    return (comparados.groupby("farmacia")
            .agg(diferenca=("diferenca_pct", "median"), produtos=("ean", "nunique"))
            .reset_index())


def mais_barata(precos):
    """Quantas vezes cada farmácia tem o menor preço (empate conta para todas as empatadas),
    só nos produtos que a Farmazids e pelo menos um concorrente vendem."""
    com_preco = precos.dropna(subset=["preco"])
    eans = com_preco.groupby("ean")["farmacia"].agg(lambda f: "farmazids" in set(f) and len(set(f)) > 1)
    com_preco = com_preco[com_preco["ean"].isin(eans[eans].index)]
    menor = com_preco.groupby("ean")["preco"].transform("min")
    vencedores = com_preco[com_preco["preco"] <= menor + EMPATE]
    contagem = vencedores.groupby("farmacia")["ean"].nunique().rename("produtos").reset_index()
    return contagem, eans.sum()


def pix_contra_cartao(precos):
    """Quanto o Pix sai mais barato que o cartão, em média, em cada farmácia."""
    ambos = precos.dropna(subset=["preco_pix", "preco_cartao"])
    ambos = ambos[ambos["preco_cartao"] > 0]
    ambos = ambos.assign(economia=(ambos["preco_cartao"] - ambos["preco_pix"]) / ambos["preco_cartao"])
    return ambos.groupby("farmacia").agg(economia=("economia", "mean"), produtos=("ean", "nunique")).reset_index()


# ---------- Promoções e estoque ----------
def estoque_e_promocoes(precos):
    conc = precos[precos["farmacia"] != "farmazids"]
    return (conc.assign(promocao=conc["desconto"] >= DESCONTO_PROMOCAO)
            .groupby("farmacia")
            .agg(produtos=("ean", "nunique"), sem_estoque=("sem_estoque", "mean"),
                 em_promocao=("promocao", "sum"), desconto_medio=("desconto", "mean"))
            .reset_index())


def maiores_descontos(precos, quantos=30):
    conc = precos[(precos["farmacia"] != "farmazids") & (precos["desconto"] >= DESCONTO_PROMOCAO)
                  & ~precos["sem_estoque"]]
    return conc.sort_values("desconto", ascending=False).head(quantos)


# ---------- Modo simulado ----------
def dados_simulados(semente=7):
    """Base inventada, com o mesmo formato das consultas, para ver o dashboard sem a AWS."""
    rng = np.random.default_rng(semente)
    remedios = ["Dipirona 500mg 10 comp", "Paracetamol 750mg 20 comp", "Ibuprofeno 400mg 10 caps",
                "Losartana 50mg 30 comp", "Omeprazol 20mg 28 caps", "Amoxicilina 500mg 21 caps",
                "Loratadina 10mg 12 comp", "Dorflex 36 comp", "Neosaldina 30 drag", "Buscopan 20 comp",
                "Vitamina C 1g 10 eferv", "Protetor solar FPS50 200ml", "Sinvastatina 20mg 30 comp",
                "Metformina 850mg 30 comp", "Rivotril 2mg 30 comp", "Allegra 120mg 10 comp",
                "Torsilax 30 comp", "Simeticona 75mg/ml 15ml", "Cimegripe 20 caps", "Benegrip 12 comp"]
    categorias = ["Medicamentos|Dor e febre", "Medicamentos|Dor e febre", "Medicamentos|Dor e febre",
                  "Medicamentos|Pressão", "Medicamentos|Estômago", "Medicamentos|Antibióticos",
                  "Medicamentos|Alergia", "Medicamentos|Dor e febre", "Medicamentos|Dor e febre",
                  "Medicamentos|Estômago", "Vitaminas", "Dermocosméticos", "Medicamentos|Colesterol",
                  "Medicamentos|Diabetes", "Medicamentos|Controlados", "Medicamentos|Alergia",
                  "Medicamentos|Dor e febre", "Medicamentos|Estômago", "Medicamentos|Gripe",
                  "Medicamentos|Gripe"]
    eans = [f"789{rng.integers(10**9, 10**10)}" for _ in remedios]
    base_preco = rng.uniform(6, 60, len(remedios)).round(2)

    linhas_precos, linhas_med = [], []
    hoje = pd.Timestamp("2026-10-21 13:31:00")
    for i, ean in enumerate(eans):
        if i != 7:  # um produto fora do catálogo da Farmazids
            linhas_med.append({"ean": ean, "nome": remedios[i], "preco_farmazids": round(base_preco[i], 2)})
        for farmacia in CONCORRENTES:
            fator = {"farmaponte": 0.97, "veracruz": 1.05, "sao_joao": 1.0, "sao_paulo": 1.08}[farmacia]
            sem_desc = round(base_preco[i] * fator * rng.uniform(1.0, 1.25), 2)
            cartao = round(sem_desc * rng.uniform(0.8, 1.0), 2)
            pix = None if farmacia == "sao_joao" else round(cartao * rng.uniform(0.93, 1.0), 2)
            em_estoque = rng.random() > 0.12
            if farmacia == "sao_paulo" and not em_estoque:
                sem_desc = SEM_ESTOQUE
            linhas_precos.append({"farmacia": farmacia, "ean": ean, "nome": remedios[i], "marca": None,
                                  "categorias": categorias[i], "preco_sem_desconto": sem_desc,
                                  "preco_cartao": cartao, "preco_pix": pix,
                                  "em_estoque": str(em_estoque).lower(), "data_coleta": str(hoje)})

    linhas_vendas, linhas_semana = [], []
    meses = pd.date_range("2025-01-01", "2026-10-01", freq="MS")
    for f in FARMACIAS_COM_VENDAS:
        tamanho = {"farmazids": 1.0, "farmaponte": 1.3, "veracruz": 0.8}[f]
        for i, ean in enumerate(eans):
            if f == "farmazids" and i == 7:
                continue
            fraco = f == "farmazids" and i in (2, 9, 15)  # produtos em que a Farmazids vende pouco
            for m in meses:
                sazonal = 1 + 0.25 * np.sin((m.month - 3) / 12 * 2 * np.pi)
                unidades = rng.poisson(40 * tamanho * sazonal * (0.3 if fraco else 1) * (1.12 if m.year == 2026 else 1))
                if m == meses[-1]:
                    unidades = int(unidades * 0.68)  # outubro pela metade
                linhas_vendas.append({"farmacia": f, "ean": ean, "mes": str(m.date()), "unidades": unidades,
                                      "vendas": max(1, unidades // 2)})
        for ano in (2025, 2026):
            for dia in range(1, 8):
                peso = [1.0, 0.95, 0.97, 1.0, 1.15, 1.3, 0.6][dia - 1]
                linhas_semana.append({"farmacia": f, "ano": ano, "dia_semana": dia,
                                      "unidades": int(4000 * tamanho * peso * rng.uniform(0.9, 1.1))})
    return {
        "vendas": pd.DataFrame(linhas_vendas),
        "semana": pd.DataFrame(linhas_semana),
        "ultima_venda": pd.DataFrame({"ultima": ["2026-10-20"]}),
        "precos": pd.DataFrame(linhas_precos),
        "medicamentos": pd.DataFrame(linhas_med),
    }


def historico_simulado(ean, semente=3):
    rng = np.random.default_rng(semente + int(chave_ean(ean) or 0) % 1000)
    dias = pd.date_range("2026-09-01", "2026-10-21", freq="D")
    linhas = []
    for f in CONCORRENTES:
        preco = rng.uniform(10, 30)
        for d in dias:
            preco *= rng.uniform(0.98, 1.02)
            linhas.append({"farmacia": f, "dia": str(d.date()), "preco_pix": None if f == "sao_joao" else round(preco, 2),
                           "preco_cartao": round(preco * 1.04, 2), "preco_sem_desconto": round(preco * 1.2, 2)})
    return pd.DataFrame(linhas)
