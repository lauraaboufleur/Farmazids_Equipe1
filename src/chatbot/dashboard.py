"""Tela do dashboard da Farmazids (a opção "Dashboard" no topo do chatbot).

As consultas e as contas ficam no dados_dashboard.py; aqui só se monta a tela.
O app.py chama mostrar_dashboard(visual), passando as cores e o idioma dos gráficos,
para o dashboard ficar com o mesmo visual do chat (claro e escuro).
"""

import io
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import altair as alt
import pandas as pd
import streamlit as st

import dados_dashboard as dd

FUSO = ZoneInfo("America/Sao_Paulo")
# Os dados novos chegam 1 vez por dia; a vw_precos_unitarios é recriada às 13:30.
# Depois deste horário, o dashboard busca tudo de novo no Athena.
HORA_ATUALIZACAO = (13, 45)

ABAS = ["Visão geral", "Vendas", "Oportunidades", "Preços", "Promoções e estoque", "Consulta de produto"]
# Cada farmácia tem sempre a mesma cor, em todos os gráficos (a ordem segue as cores da marca).
ORDEM_FARMACIAS = ["farmazids", "farmaponte", "veracruz", "sao_joao", "sao_paulo"]
DIAS_SEMANA = ["Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo"]
MESES_CURTOS = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]
VAZIO = "—"
FRASE_ESTIMADO = ("Receita estimada: unidades vendidas × preço atual de cada farmácia "
                  "(a base de vendas não tem o preço de cada venda).")


# ---------- Formatos ----------
def reais(v):
    if v is None or pd.isna(v):
        return VAZIO
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def reais_curto(v):
    """R$ 1,2 mi / R$ 350 mil, para os cartões."""
    if v is None or pd.isna(v):
        return VAZIO
    if abs(v) >= 1_000_000:
        return "R$ " + f"{v / 1_000_000:.1f}".replace(".", ",") + " mi"
    if abs(v) >= 10_000:
        return "R$ " + f"{v / 1000:.0f}" + " mil"
    return reais(v)


def inteiro(v):
    if v is None or pd.isna(v):
        return VAZIO
    return f"{v:,.0f}".replace(",", ".")


def pct(v, sinal=False):
    if v is None or pd.isna(v):
        return VAZIO
    texto = f"{v * 100:+.1f}%" if sinal else f"{v * 100:.1f}%"
    return texto.replace(".", ",")


def nome_mes(data):
    return f"{MESES_CURTOS[data.month - 1]}/{data.year}"


# ---------- Dados (guardados até a próxima atualização diária) ----------
def versao_dos_dados():
    """Muda 1 vez por dia, às 13:45: é a chave do cache, então os dados vêm de novo do Athena."""
    agora = datetime.now(FUSO)
    corte = agora.replace(hour=HORA_ATUALIZACAO[0], minute=HORA_ATUALIZACAO[1], second=0, microsecond=0)
    return (corte if agora >= corte else corte - timedelta(days=1)).isoformat()


@st.cache_data(show_spinner=False, max_entries=2)
def _dados(versao, simulado):
    base = dd.dados_simulados() if simulado else dd.carregar_tudo()
    return dd.preparar(base)


@st.cache_data(show_spinner=False, max_entries=200)
def _historico(ean, versao, simulado):
    return dd.historico_simulado(ean) if simulado else dd.carregar_historico(ean)


# ---------- Visual ----------
class Visual:
    """Cores, idioma e nomes das farmácias que vêm do app.py."""

    def __init__(self, visual):
        self.escuro = visual["escuro"]
        self.cores = visual["cores"]
        self.idioma = visual["idioma"]
        self.nomes = visual["nomes_farmacias"]
        self.celulas_escuras = visual["celulas_escuras"]
        self.simulado = visual["simulado"]

    def farmacia(self, chave):
        return self.nomes.get(chave, chave)

    def escala_farmacias(self):
        return alt.Scale(domain=[self.farmacia(f) for f in ORDEM_FARMACIAS], range=self.cores["series"])

    def cor_farmacia(self, campo="Farmácia", legenda=True):
        return alt.Color(f"{campo}:N", scale=self.escala_farmacias(), title=None,
                         legend=alt.Legend(orient="top") if legenda else None)

    def configurar(self, grafico, altura=300):
        return (
            grafico.properties(height=altura)
            .configure(background="transparent", font="Lora", locale=self.idioma)
            .configure_axis(labelColor=self.cores["texto"], titleColor=self.cores["texto"],
                            gridColor=self.cores["grade"], domainColor=self.cores["grade"],
                            tickColor=self.cores["grade"], labelLimit=220)
            .configure_legend(labelColor=self.cores["texto"])
            .configure_view(strokeWidth=0)
        )

    def grafico(self, grafico, altura=300):
        st.altair_chart(self.configurar(grafico, altura), theme=None, width="stretch")


# ---------- Peças da tela ----------
def tabela(v, df, formatos, chave, arquivo, altura=None):
    """Tabela com números em reais/percentual (continuam números: o cabeçalho ordena) e o
    botão para baixar em Excel. formatos: {coluna: "reais" | "inteiro" | "pct" | "pct_sinal"}."""
    if df.empty:
        st.info("Nada para mostrar com esses filtros.")
        return
    funcoes = {"reais": reais, "inteiro": inteiro, "pct": pct, "pct_sinal": lambda x: pct(x, sinal=True)}
    estilo = df.style.format({c: funcoes[f] for c, f in formatos.items() if c in df.columns}, na_rep=VAZIO)
    if v.escuro:
        estilo = estilo.set_properties(**v.celulas_escuras)
    st.dataframe(estilo, hide_index=True, width="stretch", height=altura or "auto",
                 column_config={"Produto": st.column_config.Column(width=300)})
    baixar_excel(df, arquivo, chave)


def baixar_excel(df, arquivo, chave):
    saida = io.BytesIO()
    try:
        df.to_excel(saida, index=False, sheet_name="Dados")
        dados, nome, tipo = (saida.getvalue(), f"{arquivo}.xlsx",
                             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except ImportError:  # sem o openpyxl instalado, vai em CSV (abre no Excel do mesmo jeito)
        dados, nome, tipo = df.to_csv(index=False, sep=";", decimal=",").encode("utf-8-sig"), f"{arquivo}.csv", "text/csv"
    st.download_button("Baixar em Excel", dados, file_name=nome, mime=tipo, key=f"baixar-{chave}",
                       icon=":material/download:")


def titulo(texto, ajuda=None):
    st.markdown(f"#### {texto}", help=ajuda)


def sem_dados(df):
    if df is None or df.empty:
        st.info("Nada para mostrar com esses filtros.")
        return True
    return False


# ---------- Abas ----------
def aba_visao_geral(v, dados, vendas_valor, ano):
    r = dd.resumo_geral(vendas_valor, ano, dados["ultima_venda"])
    periodo = f"jan–{MESES_CURTOS[r['ultimo_mes'] - 1]}/{ano}"
    comparado = f"vs. mesmo período de {ano - 1}"
    c1, c2, c3, c4 = st.columns(4)
    c1.metric(f"Receita estimada Farmazids ({periodo})", reais_curto(r["receita"]),
              pct(r["receita_var"], sinal=True) + f" {comparado}" if r["receita_var"] is not None else None)
    c2.metric(f"Unidades vendidas ({periodo})", inteiro(r["unidades"]),
              pct(r["unidades_var"], sinal=True) + f" {comparado}" if r["unidades_var"] is not None else None)
    c3.metric("Participação na receita", pct(r["participacao"]),
              help="Fatia da Farmazids na receita estimada de Farmazids, FarmaPonte e VeraCruz no período.")
    c4.metric("Posição em receita", f"{r['posicao']}º de {r['farmacias']}" if r["posicao"] else VAZIO)
    aviso = FRASE_ESTIMADO
    if r["sem_preco"]:
        aviso += f" {pct(r['sem_preco'])} das unidades não têm preço cadastrado e ficam fora do valor."
    st.caption(aviso)

    esquerda, direita = st.columns(2)
    with esquerda:
        titulo("Receita anual por farmácia")
        anual = vendas_valor.groupby(["ano", "farmacia"], as_index=False)["valor"].sum()
        if not sem_dados(anual):
            ultimo = dados["ultima_venda"]
            anual["Ano"] = anual["ano"].map(
                lambda a: f"{a} (até {MESES_CURTOS[ultimo.month - 1]})"
                if ultimo is not None and not pd.isna(ultimo) and a == ultimo.year and ultimo.month < 12 else str(a))
            anual["Farmácia"] = anual["farmacia"].map(v.farmacia)
            v.grafico(alt.Chart(anual).mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4).encode(
                x=alt.X("Ano:N", title=None, axis=alt.Axis(labelAngle=0)),
                xOffset=alt.XOffset("Farmácia:N", sort=[v.farmacia(f) for f in ORDEM_FARMACIAS]),
                y=alt.Y("valor:Q", title="Receita estimada", axis=alt.Axis(format="$,.0f")),
                color=v.cor_farmacia(),
                tooltip=[alt.Tooltip("Ano:N"), alt.Tooltip("Farmácia:N"),
                         alt.Tooltip("valor:Q", title="Receita estimada", format="$,.2f")],
            ))
    with direita:
        titulo(f"Receita mês a mês em {ano}")
        mensal = vendas_valor[vendas_valor["ano"] == ano].groupby(["mes", "farmacia"], as_index=False)["valor"].sum()
        if not sem_dados(mensal):
            mensal["Farmácia"] = mensal["farmacia"].map(v.farmacia)
            v.grafico(alt.Chart(mensal).mark_line(point=alt.OverlayMarkDef(size=70), strokeWidth=2).encode(
                x=alt.X("mes:T", title=None, axis=alt.Axis(format="%b", tickCount="month")),
                y=alt.Y("valor:Q", title="Receita estimada", axis=alt.Axis(format="$,.0f")),
                color=v.cor_farmacia(),
                tooltip=[alt.Tooltip("mes:T", title="Mês", format="%B de %Y"), alt.Tooltip("Farmácia:N"),
                         alt.Tooltip("valor:Q", title="Receita estimada", format="$,.2f")],
            ))

    titulo(f"Receita da Farmazids por categoria em {ano}",
           "Um produto pode estar em mais de uma categoria; aí ele conta em cada uma delas.")
    fz = vendas_valor[(vendas_valor["ano"] == ano) & (vendas_valor["farmacia"] == "farmazids")]
    por_categoria = (fz.merge(dados["produtos"][["ean", "categorias"]], on="ean", how="left")
                     .explode("categorias").dropna(subset=["categorias"])
                     .groupby("categorias", as_index=False)["valor"].sum()
                     .sort_values("valor", ascending=False).head(12))
    if not sem_dados(por_categoria):
        v.grafico(alt.Chart(por_categoria).mark_bar(cornerRadiusTopRight=4, cornerRadiusBottomRight=4,
                                                   color=v.cores["series"][0]).encode(
            x=alt.X("valor:Q", title="Receita estimada", axis=alt.Axis(format="$,.0f")),
            y=alt.Y("categorias:N", title=None, sort="-x"),
            tooltip=[alt.Tooltip("categorias:N", title="Categoria"),
                     alt.Tooltip("valor:Q", title="Receita estimada", format="$,.2f")],
        ), altura=max(160, 28 * len(por_categoria)))

    tabela_anual = vendas_valor.groupby(["ano", "farmacia"], as_index=False).agg(
        unidades=("unidades", "sum"), valor=("valor", "sum"))
    tabela_anual["Farmácia"] = tabela_anual["farmacia"].map(v.farmacia)
    with st.expander("Ver a tabela de receita por ano", icon=":material/table:"):
        tabela(v, tabela_anual.rename(columns={"ano": "Ano", "unidades": "Unidades", "valor": "Receita estimada"})
               [["Ano", "Farmácia", "Unidades", "Receita estimada"]],
               {"Unidades": "inteiro", "Receita estimada": "reais"}, "anual", "receita_anual")


def aba_vendas(v, dados, vendas_valor, ano):
    titulo("Unidades vendidas por mês")
    mensal = dados["vendas"].groupby(["mes", "farmacia"], as_index=False)["unidades"].sum()
    if not sem_dados(mensal):
        mensal["Farmácia"] = mensal["farmacia"].map(v.farmacia)
        v.grafico(alt.Chart(mensal).mark_line(point=alt.OverlayMarkDef(size=60), strokeWidth=2).encode(
            x=alt.X("mes:T", title=None, axis=alt.Axis(format="%b/%y", tickCount="month")),
            y=alt.Y("unidades:Q", title="Unidades", axis=alt.Axis(format=",.0f")),
            color=v.cor_farmacia(),
            tooltip=[alt.Tooltip("mes:T", title="Mês", format="%B de %Y"), alt.Tooltip("Farmácia:N"),
                     alt.Tooltip("unidades:Q", title="Unidades", format=",.0f")],
        ))
        ultimo = dados["ultima_venda"]
        if ultimo is not None and not pd.isna(ultimo):
            st.caption(f"Vendas registradas até {ultimo:%d/%m/%Y}: o último mês pode estar incompleto.")

    esquerda, direita = st.columns(2)
    with esquerda:
        titulo(f"Vendas por dia da semana em {ano}", "Este gráfico não usa o filtro de categoria.")
        semana = dados["semana"][dados["semana"]["ano"] == ano].copy()
        if not sem_dados(semana):
            semana["Dia"] = semana["dia_semana"].astype(int).map(lambda d: DIAS_SEMANA[d - 1])
            semana["Farmácia"] = semana["farmacia"].map(v.farmacia)
            v.grafico(alt.Chart(semana).mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3).encode(
                x=alt.X("Dia:N", sort=DIAS_SEMANA, title=None, axis=alt.Axis(labelAngle=0)),
                xOffset=alt.XOffset("Farmácia:N", sort=[v.farmacia(f) for f in ORDEM_FARMACIAS]),
                y=alt.Y("unidades:Q", title="Unidades", axis=alt.Axis(format=",.0f")),
                color=v.cor_farmacia(),
                tooltip=[alt.Tooltip("Dia:N"), alt.Tooltip("Farmácia:N"),
                         alt.Tooltip("unidades:Q", title="Unidades", format=",.0f")],
            ))
    with direita:
        titulo(f"10 produtos mais vendidos em {ano}")
        farmacia = st.segmented_control("Farmácia", dd.FARMACIAS_COM_VENDAS, default="farmazids",
                                        format_func=v.farmacia, key="dash-top-farmacia",
                                        label_visibility="collapsed") or "farmazids"
        top = (vendas_valor[(vendas_valor["ano"] == ano) & (vendas_valor["farmacia"] == farmacia)]
               .groupby("ean", as_index=False).agg(unidades=("unidades", "sum"), valor=("valor", "sum"))
               .merge(dados["produtos"][["ean", "nome"]], on="ean", how="left")
               .sort_values("unidades", ascending=False).head(10))
        if not sem_dados(top):
            cor = v.cores["series"][ORDEM_FARMACIAS.index(farmacia)]
            v.grafico(alt.Chart(top).mark_bar(cornerRadiusTopRight=4, cornerRadiusBottomRight=4, color=cor).encode(
                x=alt.X("unidades:Q", title="Unidades", axis=alt.Axis(format=",.0f")),
                y=alt.Y("nome:N", title=None, sort="-x"),
                tooltip=[alt.Tooltip("nome:N", title="Produto"),
                         alt.Tooltip("unidades:Q", title="Unidades", format=",.0f"),
                         alt.Tooltip("valor:Q", title="Receita estimada", format="$,.2f")],
            ), altura=300)

    variacao, antes, depois = dd.variacao_mensal(dados["vendas"], dados["produtos"], dados["ultima_venda"])
    if antes is None:
        return
    nomes = {"nome": "Produto", "antes": f"Unidades em {nome_mes(antes)}",
             "depois": f"Unidades em {nome_mes(depois)}", "variacao": "Variação"}
    formatos = {nomes["antes"]: "inteiro", nomes["depois"]: "inteiro", "Variação": "pct_sinal"}
    esquerda, direita = st.columns(2)
    with esquerda:
        titulo("Produtos da Farmazids em queda",
               f"Comparação entre os dois últimos meses completos: {nome_mes(antes)} e {nome_mes(depois)}.")
        queda = variacao[variacao["variacao"] < 0].sort_values("variacao")
        tabela(v, queda[list(nomes)].rename(columns=nomes), formatos, "queda", "produtos_em_queda", altura=320)
    with direita:
        titulo("Produtos da Farmazids em alta",
               f"Comparação entre os dois últimos meses completos: {nome_mes(antes)} e {nome_mes(depois)}.")
        alta = variacao[variacao["variacao"] > 0].sort_values("variacao", ascending=False)
        tabela(v, alta[list(nomes)].rename(columns=nomes), formatos, "alta", "produtos_em_alta", altura=320)


def aba_oportunidades(v, dados, vendas_valor, ano):
    comparacao = dd.comparacao_de_vendas(vendas_valor, dados["precos"], dados["produtos"], ano)

    titulo(f"Produtos que a Farmazids vende pouco em {ano}",
           "Comparação com a média de FarmaPonte e VeraCruz (as farmácias da base de vendas). "
           "A receita que falta é estimada com o preço da Farmazids.")
    c1, c2 = st.columns([2, 1])
    limite = c1.slider("Mostrar quando a Farmazids vende menos que esta parte da média dos concorrentes",
                       10, 90, 50, step=10, format="%d%%", key="dash-limite") / 100
    minimo = c2.number_input("Mínimo de unidades vendidas pelos concorrentes", 1, 10_000, 10, key="dash-minimo")
    pouco = dd.vendendo_pouco(comparacao, limite, minimo)
    if not pouco.empty:
        m1, m2, m3 = st.columns(3)
        m1.metric("Produtos encontrados", inteiro(len(pouco)))
        m2.metric("Receita que falta (estimada)", reais_curto(pouco["receita_potencial"].sum()))
        m3.metric("Desses, a Farmazids está mais cara em", pct(pouco["mais_cara"].mean()))
    exibir = pouco.assign(
        onde=pouco["onde"].map(lambda f: v.farmacia(f) if isinstance(f, str) else VAZIO),
        mais_cara=pouco["mais_cara"].map({True: "Sim", False: "Não"}))
    nomes = {"nome": "Produto", "farmazids": "Farmazids (un.)", "media_concorrentes": "Média concorrentes (un.)",
             "proporcao": "Farmazids / média", "preco_farmazids": "Preço Farmazids",
             "menor_concorrente": "Menor preço concorrente", "onde": "Onde", "diferenca_pct": "Diferença de preço",
             "mais_cara": "Farmazids mais cara?", "receita_potencial": "Receita que falta"}
    tabela(v, exibir[list(nomes)].rename(columns=nomes),
           {"Farmazids (un.)": "inteiro", "Média concorrentes (un.)": "inteiro", "Farmazids / média": "pct",
            "Preço Farmazids": "reais", "Menor preço concorrente": "reais", "Diferença de preço": "pct_sinal",
            "Receita que falta": "reais"}, "pouco", "farmazids_vende_pouco", altura=380)

    esquerda, direita = st.columns(2)
    with esquerda:
        titulo("Concorrente sem estoque agora",
               "Produtos do catálogo da Farmazids que algum concorrente está sem estoque na última coleta: "
               "chance de vender mais.")
        sem = dd.concorrente_sem_estoque(dados["precos"], comparacao)
        sem = sem.assign(farmacias_sem_estoque=sem["farmacias_sem_estoque"].map(
            lambda f: ", ".join(v.farmacia(x) for x in f)))
        nomes = {"nome": "Produto", "farmacias_sem_estoque": "Sem estoque em",
                 "farmazids": f"Vendas Farmazids {ano} (un.)", "preco_farmazids": "Preço Farmazids"}
        tabela(v, sem[list(nomes)].rename(columns=nomes),
               {nomes["farmazids"]: "inteiro", "Preço Farmazids": "reais"}, "sem-estoque",
               "concorrente_sem_estoque", altura=320)
    with direita:
        titulo("Vendidos pelos concorrentes, fora do catálogo da Farmazids",
               "Produtos que FarmaPonte ou VeraCruz vendem e a Farmazids não tem no catálogo.")
        fora = dd.fora_do_catalogo(comparacao)
        nomes = {"nome": "Produto", "ean": "EAN", "media_concorrentes": "Média concorrentes (un.)",
                 "menor_concorrente": "Menor preço concorrente"}
        tabela(v, fora[list(nomes)].rename(columns=nomes),
               {"Média concorrentes (un.)": "inteiro", "Menor preço concorrente": "reais"}, "fora",
               "fora_do_catalogo", altura=320)


def aba_precos(v, dados):
    st.caption("Preço de hoje (última coleta): o Pix; se a farmácia não informa o Pix, o do cartão. "
               "Para a Farmazids, o preço do catálogo. Produtos sem estoque não entram.")
    precos = dados["precos"][~dados["precos"]["sem_estoque"]]
    contagem, comparados = dd.mais_barata(precos)
    posicao = dd.posicao_de_preco(precos).dropna(subset=["preco_farmazids", "menor_concorrente"])
    mais_cara = posicao[posicao["diferenca"] > dd.EMPATE]
    vence = contagem.set_index("farmacia")["produtos"].get("farmazids", 0)
    c1, c2, c3 = st.columns(3)
    c1.metric("Farmazids com o menor preço", pct(vence / comparados if comparados else None),
              help="Nos produtos que a Farmazids e pelo menos um concorrente vendem. Empate conta.")
    c2.metric("Produtos comparados", inteiro(comparados))
    c3.metric("Farmazids mais cara que algum concorrente", inteiro(len(mais_cara)))

    esquerda, direita = st.columns(2)
    with esquerda:
        titulo("Quanto cada concorrente cobra a mais que a Farmazids",
               "Mediana da diferença nos produtos que os dois vendem. Acima de zero: o concorrente é mais caro; "
               "abaixo: mais barato que a Farmazids.")
        indice = dd.indice_de_preco(dd.comparar_precos(precos))
        if not sem_dados(indice):
            indice["Farmácia"] = indice["farmacia"].map(v.farmacia)
            indice["Situação"] = indice["diferenca"].map(lambda d: "Mais caro que a Farmazids" if d > 0
                                                         else "Mais barato que a Farmazids")
            barras = alt.Chart(indice).mark_bar(cornerRadius=4).encode(
                x=alt.X("Farmácia:N", title=None, sort="-y", axis=alt.Axis(labelAngle=0)),
                y=alt.Y("diferenca:Q", title="Diferença para a Farmazids", axis=alt.Axis(format="+.0%")),
                color=alt.Color("Situação:N", title=None, legend=alt.Legend(orient="top"),
                                scale=alt.Scale(domain=["Mais caro que a Farmazids", "Mais barato que a Farmazids"],
                                                range=[v.cores["series"][0], v.cores["series"][1]])),
                tooltip=[alt.Tooltip("Farmácia:N"), alt.Tooltip("diferenca:Q", title="Diferença", format="+.1%"),
                         alt.Tooltip("produtos:Q", title="Produtos comparados")],
            )
            zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color=v.cores["texto"], opacity=0.5).encode(y="y:Q")
            v.grafico(barras + zero)
    with direita:
        titulo("Quem tem o menor preço", "Em quantos produtos cada farmácia tem o menor preço (empate conta para todas).")
        if not sem_dados(contagem):
            contagem["Farmácia"] = contagem["farmacia"].map(v.farmacia)
            v.grafico(alt.Chart(contagem).mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4).encode(
                x=alt.X("Farmácia:N", title=None, sort="-y", axis=alt.Axis(labelAngle=0)),
                y=alt.Y("produtos:Q", title="Produtos", axis=alt.Axis(format=",.0f")),
                color=v.cor_farmacia(legenda=False),
                tooltip=[alt.Tooltip("Farmácia:N"), alt.Tooltip("produtos:Q", title="Produtos com o menor preço")],
            ))

    titulo("Produtos em que a Farmazids está mais cara")
    exibir = (mais_cara.merge(dados["produtos"][["ean", "nome"]], on="ean", how="left")
              .sort_values("diferenca_pct", ascending=False))
    exibir["onde"] = exibir["onde"].map(v.farmacia)
    nomes = {"nome": "Produto", "preco_farmazids": "Preço Farmazids", "menor_concorrente": "Menor preço concorrente",
             "onde": "Onde", "diferenca": "Diferença (R$)", "diferenca_pct": "Diferença (%)"}
    tabela(v, exibir[list(nomes)].rename(columns=nomes),
           {"Preço Farmazids": "reais", "Menor preço concorrente": "reais", "Diferença (R$)": "reais",
            "Diferença (%)": "pct_sinal"}, "mais-cara", "farmazids_mais_cara", altura=360)

    titulo("Quanto o Pix sai mais barato que o cartão", "Média da economia no Pix em relação ao cartão, "
           "nos produtos com os dois preços. A São João não informa o preço Pix.")
    pix = dd.pix_contra_cartao(dados["precos"])
    if not sem_dados(pix):
        pix["Farmácia"] = pix["farmacia"].map(v.farmacia)
        v.grafico(alt.Chart(pix).mark_bar(cornerRadiusTopRight=4, cornerRadiusBottomRight=4).encode(
            x=alt.X("economia:Q", title="Economia média no Pix", axis=alt.Axis(format=".0%")),
            y=alt.Y("Farmácia:N", title=None, sort="-x"),
            color=v.cor_farmacia(legenda=False),
            tooltip=[alt.Tooltip("Farmácia:N"), alt.Tooltip("economia:Q", title="Economia média", format=".1%"),
                     alt.Tooltip("produtos:Q", title="Produtos")],
        ), altura=max(120, 40 * len(pix)))


def aba_promocoes(v, dados):
    st.caption(f"Última coleta de preços. Em promoção: desconto de pelo menos {pct(dd.DESCONTO_PROMOCAO)} "
               "sobre o preço sem desconto (no Pix ou no cartão, o que for menor).")
    resumo = dd.estoque_e_promocoes(dados["precos"])
    if sem_dados(resumo):
        return
    resumo["Farmácia"] = resumo["farmacia"].map(v.farmacia)
    esquerda, direita = st.columns(2)
    with esquerda:
        titulo("Produtos sem estoque", "Inclui os produtos em que a Drogaria São Paulo grava 9999876 no preço.")
        v.grafico(alt.Chart(resumo).mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4).encode(
            x=alt.X("Farmácia:N", title=None, sort="-y", axis=alt.Axis(labelAngle=0)),
            y=alt.Y("sem_estoque:Q", title="Parte dos produtos", axis=alt.Axis(format=".0%")),
            color=v.cor_farmacia(legenda=False),
            tooltip=[alt.Tooltip("Farmácia:N"), alt.Tooltip("sem_estoque:Q", title="Sem estoque", format=".1%"),
                     alt.Tooltip("produtos:Q", title="Produtos coletados")],
        ))
    with direita:
        titulo("Produtos em promoção")
        v.grafico(alt.Chart(resumo).mark_bar(cornerRadiusTopLeft=4, cornerRadiusTopRight=4).encode(
            x=alt.X("Farmácia:N", title=None, sort="-y", axis=alt.Axis(labelAngle=0)),
            y=alt.Y("em_promocao:Q", title="Produtos", axis=alt.Axis(format=",.0f")),
            color=v.cor_farmacia(legenda=False),
            tooltip=[alt.Tooltip("Farmácia:N"), alt.Tooltip("em_promocao:Q", title="Em promoção"),
                     alt.Tooltip("desconto_medio:Q", title="Desconto médio", format=".1%")],
        ))

    titulo("Maiores descontos agora")
    descontos = dd.maiores_descontos(dados["precos"]).assign(
        farmacia=lambda d: d["farmacia"].map(v.farmacia),
        tem_na_farmazids=lambda d: d["ean"].isin(dados["produtos"].loc[dados["produtos"]["no_catalogo"], "ean"])
        .map({True: "Sim", False: "Não"}))
    nomes = {"farmacia": "Farmácia", "nome": "Produto", "preco_sem_desconto": "Preço sem desconto",
             "preco_pix": "Preço Pix", "preco_cartao": "Preço cartão", "desconto": "Desconto",
             "tem_na_farmazids": "A Farmazids vende?"}
    tabela(v, descontos[list(nomes)].rename(columns=nomes),
           {"Preço sem desconto": "reais", "Preço Pix": "reais", "Preço cartão": "reais", "Desconto": "pct"},
           "descontos", "maiores_descontos", altura=380)


def aba_produto(v, dados, vendas_valor, ano, versao):
    produtos = dados["produtos"].sort_values("nome")
    rotulos = dict(zip(produtos["ean"], produtos["nome"] + " · " + produtos["ean"]))
    ean = st.selectbox("Produto", list(rotulos), index=None, format_func=rotulos.get, key="dash-produto",
                       placeholder="Digite o nome ou o código de barras do produto")
    if not ean:
        st.caption("Escolha um produto para ver vendas, preços de hoje nas 5 farmácias e a evolução do preço.")
        return

    precos = dados["precos"][dados["precos"]["ean"] == ean]
    fz = precos.loc[precos["farmacia"] == "farmazids", "preco"]
    conc = precos[(precos["farmacia"] != "farmazids") & ~precos["sem_estoque"]].dropna(subset=["preco"])
    menor = conc.loc[conc["preco"].idxmin()] if not conc.empty else None
    vendido = vendas_valor[(vendas_valor["ean"] == ean) & (vendas_valor["ano"] == ano)]
    c1, c2, c3 = st.columns(3)
    c1.metric("Preço Farmazids", reais(fz.iat[0]) if len(fz) else "Fora do catálogo")
    c2.metric(f"Menor preço concorrente ({v.farmacia(menor['farmacia'])})" if menor is not None
              else "Menor preço concorrente", reais(menor["preco"]) if menor is not None else VAZIO)
    c3.metric(f"Unidades vendidas pela Farmazids em {ano}",
              inteiro(vendido.loc[vendido["farmacia"] == "farmazids", "unidades"].sum()))

    titulo("Preços de hoje")
    hoje = precos.assign(
        Farmácia=precos["farmacia"].map(v.farmacia),
        Estoque=precos["sem_estoque"].map({True: "✗", False: "✓"}),
        Coleta=precos["coleta"].map(lambda c: f"{c:%d/%m/%Y %H:%M}" if not pd.isna(c) else VAZIO)
        if "coleta" in precos else VAZIO,
    ).sort_values("preco")
    nomes = {"Farmácia": "Farmácia", "preco": "Preço (Pix ou catálogo)", "preco_cartao": "Preço cartão",
             "preco_sem_desconto": "Preço sem desconto", "Estoque": "Estoque", "Coleta": "Coleta"}
    tabela(v, hoje[[c for c in nomes if c in hoje.columns]].rename(columns=nomes),
           {"Preço (Pix ou catálogo)": "reais", "Preço cartão": "reais", "Preço sem desconto": "reais"},
           "produto-precos", f"precos_{ean}")

    esquerda, direita = st.columns(2)
    with esquerda:
        titulo("Unidades vendidas por mês")
        mensal = dados["vendas"][dados["vendas"]["ean"] == ean].groupby(["mes", "farmacia"], as_index=False)["unidades"].sum()
        if not sem_dados(mensal):
            mensal["Farmácia"] = mensal["farmacia"].map(v.farmacia)
            v.grafico(alt.Chart(mensal).mark_line(point=alt.OverlayMarkDef(size=60), strokeWidth=2).encode(
                x=alt.X("mes:T", title=None, axis=alt.Axis(format="%b/%y", tickCount="month")),
                y=alt.Y("unidades:Q", title="Unidades", axis=alt.Axis(format=",.0f")),
                color=v.cor_farmacia(),
                tooltip=[alt.Tooltip("mes:T", title="Mês", format="%B de %Y"), alt.Tooltip("Farmácia:N"),
                         alt.Tooltip("unidades:Q", title="Unidades")],
            ))
    with direita:
        titulo("Evolução do preço", "Pix; se a farmácia não informa o Pix, o cartão. "
               "A linha tracejada é o preço de hoje da Farmazids.")
        try:
            with st.spinner("Buscando o histórico de preços..."):
                historico = _historico(ean, versao, v.simulado)
        except Exception as erro:
            print(f"[dashboard] histórico falhou: {erro!r}")
            st.warning("Não consegui buscar o histórico de preços agora. Tente de novo em instantes.")
            return
        historico = historico.assign(
            farmacia=historico["farmacia"].map(dd.chave_farmacia),
            preco=pd.to_numeric(historico["preco_pix"], errors="coerce")
            .fillna(pd.to_numeric(historico["preco_cartao"], errors="coerce")),
            dia=pd.to_datetime(historico["dia"], errors="coerce"),
        ).dropna(subset=["preco", "dia"])
        historico = historico[(historico["preco"] != dd.SEM_ESTOQUE) & (historico["farmacia"] != "farmazids")]
        if not sem_dados(historico):
            historico["Farmácia"] = historico["farmacia"].map(v.farmacia)
            linhas = alt.Chart(historico).mark_line(point=alt.OverlayMarkDef(size=40), strokeWidth=2).encode(
                x=alt.X("dia:T", title=None, axis=alt.Axis(format="%d/%m")),
                y=alt.Y("preco:Q", title="Preço", axis=alt.Axis(format="$,.2f"), scale=alt.Scale(zero=False)),
                color=v.cor_farmacia(),
                tooltip=[alt.Tooltip("dia:T", title="Dia", format="%d/%m/%Y"), alt.Tooltip("Farmácia:N"),
                         alt.Tooltip("preco:Q", title="Preço", format="$,.2f")],
            )
            if len(fz) and not pd.isna(fz.iat[0]):
                linhas += alt.Chart(pd.DataFrame({"preco": [fz.iat[0]]})).mark_rule(
                    color=v.cores["series"][0], strokeDash=[6, 4], strokeWidth=2).encode(
                    y="preco:Q", tooltip=[alt.Tooltip("preco:Q", title="Farmazids hoje", format="$,.2f")])
            v.grafico(linhas)


# ---------- Página ----------
def estilo(v):
    """Cartões de número no mesmo visual do chat (o tema do Streamlit é sempre claro)."""
    fundo = "#261C33" if v.escuro else "#FFFFFF"
    borda = v.cores["grade"]
    st.html(f"""<style>
    .st-key-dashboard [data-testid="stMetric"] {{
        background: {fundo}; border: 1px solid {borda}; border-radius: 0.6rem; padding: 0.8rem 1rem;
    }}
    .st-key-dashboard [data-testid="stMetricValue"],
    .st-key-dashboard [data-testid="stMetricLabel"] p,
    .st-key-dashboard h4 {{ color: {v.cores["texto"]}; }}
    .st-key-dashboard [data-testid="stMetricLabel"] p {{ white-space: normal; }}
    /* Abas, rótulos, legendas, caixas de escolha e botões: o tema do Streamlit é sempre claro,
       então as cores vêm das variáveis do estilo.css (o estilo-escuro.css troca no modo escuro). */
    .st-key-dashboard button[role="tab"] p,
    .st-key-dashboard [data-testid="stWidgetLabel"] p,
    .st-key-dashboard [data-testid="stExpander"] summary p,
    .st-key-dashboard [data-testid="stMarkdownContainer"] p {{ color: {v.cores["texto"]}; }}
    .st-key-dashboard [data-testid="stCaptionContainer"] p {{ color: var(--cor-hora); }}
    .st-key-dashboard button[role="tab"][aria-selected="true"] p {{ color: var(--cor-lema); }}
    .st-key-dashboard [data-baseweb="select"] > div,
    .st-key-dashboard [data-baseweb="input"],
    .st-key-dashboard [data-baseweb="input"] input,
    .st-key-dashboard [data-testid="stExpander"] details {{
        background: var(--fundo-caixa); color: var(--texto-assistente); border-color: var(--divisoria-caixa);
    }}
    .st-key-dashboard [data-baseweb="select"] input,
    .st-key-dashboard [data-baseweb="select"] div {{ color: var(--texto-assistente); }}
    .st-key-dashboard [data-testid="stBaseButton-secondary"],
    .st-key-dashboard [data-testid="stBaseButton-segmented_control"] {{
        background: var(--fundo-caixa); color: var(--texto-assistente); border-color: var(--divisoria-caixa);
    }}
    .st-key-dashboard [data-testid="stBaseButton-segmented_controlActive"] {{
        background: var(--botao-enviar); color: #FFFFFF; border-color: var(--botao-enviar);
    }}
    .st-key-dashboard button p {{ color: inherit; }}
    </style>""")


def mostrar_dashboard(visual):
    v = Visual(visual)
    versao = versao_dos_dados()
    with st.container(key="dashboard"):
        estilo(v)
        try:
            with st.spinner("Carregando os dados do Athena (leva alguns segundos na primeira vez do dia)..."):
                dados = _dados(versao, v.simulado)
        except Exception as erro:
            print(f"[dashboard] erro ao carregar: {erro!r}")
            st.error("Não consegui carregar os dados do dashboard. Confira se as credenciais da AWS têm "
                     "permissão no Athena, no Glue e no bucket (iam-policy-dashboard.json) e tente de novo.")
            if st.button("Tentar de novo", key="dash-tentar"):
                _dados.clear()
                st.rerun()
            return

        anos = sorted(dados["vendas"]["ano"].dropna().astype(int).unique(), reverse=True)
        if not anos:
            st.info("A base de vendas está vazia.")
            return
        c1, c2, c3 = st.columns([1, 3, 1], vertical_alignment="bottom")
        ano = c1.selectbox("Ano", anos, key="dash-ano")
        categorias = c2.multiselect("Categorias", dd.todas_categorias(dados["produtos"]), key="dash-categorias",
                                    placeholder="Todas as categorias")
        if c3.button("Atualizar dados", key="dash-atualizar", icon=":material/refresh:", width="stretch",
                     help="Busca tudo de novo no Athena (normalmente atualiza sozinho 1 vez por dia, às 13:45)."):
            _dados.clear()
            _historico.clear()
            st.rerun()
        datas = []
        if dados["ultima_venda"] is not None and not pd.isna(dados["ultima_venda"]):
            datas.append(f"vendas até {dados['ultima_venda']:%d/%m/%Y}")
        if dados["ultima_coleta"] is not None and not pd.isna(dados["ultima_coleta"]):
            datas.append(f"preços coletados em {dados['ultima_coleta']:%d/%m/%Y às %H:%M}")
        if datas:
            st.caption("Dados: " + " · ".join(datas) + (" · modo simulado (dados de teste)" if v.simulado else ""))

        dados = dd.filtrar_categorias(dados, categorias)
        vendas_valor = dd.receita(dados)
        abas = st.tabs(ABAS)
        with abas[0]:
            aba_visao_geral(v, dados, vendas_valor, ano)
        with abas[1]:
            aba_vendas(v, dados, vendas_valor, ano)
        with abas[2]:
            aba_oportunidades(v, dados, vendas_valor, ano)
        with abas[3]:
            aba_precos(v, dados)
        with abas[4]:
            aba_promocoes(v, dados)
        with abas[5]:
            aba_produto(v, dados, vendas_valor, ano, versao)
