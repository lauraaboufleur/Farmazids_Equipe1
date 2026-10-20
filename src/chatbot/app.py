"""Tela do chatbot da Farmazids.

Rodar a partir da raiz do repositório (é lá que fica o .streamlit/config.toml
com as cores e fontes da marca):
    streamlit run src/chatbot/app.py

O visual segue o template aprovado: o estilo fica em assets/estilo.css e as
imagens em assets/. Este arquivo só monta a tela e conversa com o backend.
"""

import base64
import hmac
import html
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import altair as alt
import pandas as pd
import streamlit as st

from cliente_chatbot import MODO_SIMULADO, perguntar, sintetizar

ASSETS = Path(__file__).parent / "assets"
LOGO = str(ASSETS / "logo_farmazids.png")                    # símbolo redondo
TITULO = str(ASSETS / "titulo_farmazids.png")                # letreiro roxo e dourado (fundo claro)
TITULO_ESCURO = str(ASSETS / "titulo_farmazids_escuro.png")  # mesmo letreiro com as letras roxas em branco
FUSO = ZoneInfo("America/Sao_Paulo")

# Colunas que parecem número, mas são códigos (não podem perder zeros à esquerda).
COLUNAS_TEXTO = {"ean", "sku", "id_venda"}

# Nomes das colunas na tabela. O que não estiver aqui vira "Nome com espaços".
NOMES_COLUNAS = {
    "farmacia": "Farmácia",
    "nome": "Produto",
    "marca": "Marca",
    "ean": "EAN",
    "sku": "SKU",
    "preco_pix": "Preço Pix",
    "preco_cartao": "Preço cartão",
    "preco_sem_desconto": "Preço sem desconto",
    "preco": "Preço",
    "preco_medio": "Preço médio",
    "menor_preco": "Menor preço",
    "menor_preco_pix": "Menor preço Pix",
    "menor_preco_cartao": "Menor preço cartão",
    "maior_preco": "Maior preço",
    "em_estoque": "Estoque",
    "data_coleta": "Coleta",
    "dt": "Coleta",
    "desconto": "Desconto",
    "quantidade": "Quantidade",
    "unidade": "Unidade",
    "concentracao": "Concentração",
    "forma": "Forma",
}

# Preço por unidade (vw_precos_unitarios): o cabeçalho diz a unidade ("Por comprimido",
# "Pix por ml"...) quando todas as linhas têm a mesma; senão, "Por unidade".
PRECO_POR_UNIDADE = re.compile(r"^(?:menor_)?preco(?:_(pix|cartao|farmazids))?_por_unidade$")
TIPOS_PRECO = {"pix": "Pix por", "cartao": "Cartão por", "farmazids": "Farmazids por"}

# Unidade e forma vêm sem acento das views; na tabela aparecem escritas direito.
NOMES_UNIDADES_FORMAS = {
    "capsula": "cápsula",
    "dragea": "drágea",
    "sache": "sachê",
    "supositorio": "supositório",
    "ovulo": "óvulo",
    "solucao": "solução",
    "suspensao": "suspensão",
    "colirio": "colírio",
    "locao": "loção",
    "po": "pó",
    "injetavel": "injetável",
    "comprimido liberacao prolongada": "comprimido de liberação prolongada",
    "capsula liberacao prolongada": "cápsula de liberação prolongada",
    "dragea liberacao prolongada": "drágea de liberação prolongada",
    "capsula efervescente": "cápsula efervescente",
}

# Larguras fixas para a tabela caber no balão sem rolar para o lado. O nome do
# produto pode ficar cortado; arrastando a borda do cabeçalho, a coluna aumenta.
LARGURAS = {"Produto": st.column_config.Column(width=300),
            "Estoque": st.column_config.TextColumn(width=74, alignment="center"),
            "Coleta": st.column_config.Column(width=100)}

# Como sim/não aparece na tabela (ex.: coluna Estoque).
MARCAS = {"true": "✓", "false": "✗"}

# O que aparece quando falta a informação (preço, estoque...), na tabela e no texto.
VAZIO = "—"
TEXTOS_VAZIOS = {"", "none", "null", "nan", "nat", "<na>"}
# As mesmas palavras soltas no texto do Bedrock (ex.: "custa None") viram "—".
PALAVRAS_VAZIAS = re.compile(r"\b(?:None|none|NaN|nan|null|NULL|NaT|<NA>)\b")

# Como os nomes das farmácias aparecem na tabela.
NOMES_FARMACIAS = {
    "farmaponte": "FarmaPonte",
    "veracruz": "Drogaria VeraCruz",
    "sao_joao": "São João",
    "sao_paulo": "Drogaria São Paulo",
    "farmazids": "Farmazids",
}

BOAS_VINDAS = (
    "**Olá!** Sou o **assistente de dados da Farmazids.**\n\n"
    "Posso te ajudar a consultar preços, comparar com concorrentes, analisar "
    "vendas e obter informações sobre nossos medicamentos.\n\n"
    "Faça sua pergunta na caixa abaixo."
)

# Voz do navegador (reserva do Polly): idioma, pausa entre as frases e as vozes em ordem
# de prioridade. Fica a primeira voz pt-BR do aparelho cujo nome contém "nome"; se nenhuma
# existir, a pt-BR padrão do sistema, com a velocidade e o tom de "padrao".
# É a única configuração da voz: o assets/voz.js recebe estes valores.
VOZ = {
    "idioma": "pt-BR",
    "pausa_entre_frases": 250,  # ms
    "vozes": [
        # Neurais do Edge: as mais naturais.
        {"nome": "Microsoft Francisca Online (Natural)", "velocidade": 1.05, "tom": 1.0},
        {"nome": "Microsoft Thalita Online (Natural)", "velocidade": 1.05, "tom": 1.0},
        {"nome": "Natural", "velocidade": 1.05, "tom": 1.0},
        {"nome": "Online", "velocidade": 1.05, "tom": 1.0},
        # Chrome.
        {"nome": "Google português do Brasil", "velocidade": 1.0, "tom": 1.0},
    ],
    "padrao": {"velocidade": 1.0, "tom": 1.0},
}
# Voz mais natural: Amazon Polly (voz neural pt-BR). Se ele falhar, a tela usa a voz
# do navegador (VOZ, acima). Velocidade e pausa entre as frases vão no SSML.
POLLY_VOZ = "Camila"
POLLY_VELOCIDADE = "105%"
POLLY_PAUSA = "300ms"
POLLY_ESPERA_FALHA = 600  # depois de uma falha, quantos segundos esperar antes de tentar o Polly de novo

# Depois desse tempo, o balão de espera explica por que está demorando.
SEGUNDOS_AVISO_DEMORA = 15

# Memória da conversa: quantas trocas anteriores vão junto com a pergunta e o
# tamanho máximo de cada parte. Texto curto para não deixar a resposta lenta.
TROCAS_NO_HISTORICO = 3
CARACTERES_RESPOSTA = 300
CARACTERES_SQL = 1500

# Gráfico abaixo da tabela, só quando a pergunta pede e a redigir-resposta sugere um.
TIPOS_GRAFICO = {"barras", "linha"}
SEM_ESTOQUE = 9999876  # "preço" que a São Paulo grava quando está fora de estoque: não entra no gráfico
PEDIU_GRAFICO = re.compile(r"gr[áa]fico", re.IGNORECASE)
COLUNAS_PRODUTO = {"nome", "produto", "nome_produto"}  # mostrada na dica do mouse junto com o menor preço
# Cores da marca, uma por série (farmácia, Pix/cartão...). No modo escuro o
# roxo some sobre o fundo, então a primeira cor vira um lilás claro.
CORES_GRAFICO = {
    "claro": {"series": ["#482960", "#C2A05A", "#8E6BAE", "#8C7340", "#B9A6CC"],
              "texto": "#2E1F3E", "grade": "#E6DAEE"},
    "escuro": {"series": ["#B79AD6", "#C2A05A", "#8E6BAE", "#E3CFA0", "#6E5A84"],
               "texto": "#EDE6F2", "grade": "#3A2C4A"},
}
# Números e datas no padrão brasileiro dentro do gráfico (R$ 4,29 e 16/10/2026).
IDIOMA_GRAFICO = alt.Locale(
    number=alt.NumberLocale(decimal=",", thousands=".", grouping=[3], currency=["R$ ", ""]),
    time=alt.TimeLocale(
        dateTime="%A, %e de %B de %Y. %X", date="%d/%m/%Y", time="%H:%M:%S", periods=["AM", "PM"],
        days=["Domingo", "Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado"],
        shortDays=["Dom", "Seg", "Ter", "Qua", "Qui", "Sex", "Sáb"],
        months=["Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho", "Julho", "Agosto",
                "Setembro", "Outubro", "Novembro", "Dezembro"],
        shortMonths=["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"],
    ),
)

# Cores das células da tabela no modo escuro. A tabela é desenhada pelo
# Streamlit, sem passar pelo CSS, então a cor vai pelo Styler. A linha de
# títulos não aceita cor pelo Styler e continua lilás clara.
CELULAS_ESCURAS = {"background-color": "#261C33", "color": "#EDE6F2"}


def agora():
    return datetime.now(FUSO).strftime("%H:%M")


def modo_escuro():
    return st.session_state.tema == "escuro"


def trocar_tema():
    st.session_state.tema = "claro" if modo_escuro() else "escuro"


def svg_como_url(nome):
    dados = base64.b64encode((ASSETS / "decoracao" / nome).read_bytes()).decode()
    return f'url("data:image/svg+xml;base64,{dados}")'


def aplicar_estilo():
    variaveis = (
        ":root {"
        f"--pessoa: {svg_como_url('pessoa.svg')};"
        f"--aviao: {svg_como_url('aviao.svg')};"
        "}"
    )
    css = (ASSETS / "estilo.css").read_text(encoding="utf-8")
    # No modo escuro, o estilo-escuro.css troca as cores do estilo.css.
    if modo_escuro():
        css += "\n" + (ASSETS / "estilo-escuro.css").read_text(encoding="utf-8")
    st.html(f"<style>{variaveis}\n{css}</style>")


def instalar_voz():
    """Microfone na caixa de pergunta e botões "Ouvir" (assets/voz.js). O script se
    instala uma vez por página; o container fica escondido (ver .st-key-voz no CSS)."""
    codigo = (ASSETS / "voz.js").read_text(encoding="utf-8").replace("__CONFIG__", json.dumps(VOZ))
    # O Streamlit (DOMPurify) apaga, sem avisar, um script cujo texto tenha o sinal de
    # menor seguido de letra ou barra. Melhor falhar aqui do que a voz sumir em silêncio.
    if re.search(r"<[/A-Za-z]", codigo):
        raise ValueError("voz.js: escreva o sinal de menor como \\x3c (ver o comentário no arquivo).")
    with st.container(key="voz"):
        st.html(f"<script>{codigo}</script>", unsafe_allow_javascript=True)


# ---------- Login ----------
def senha_confere(usuario, senha):
    """Os usuários e senhas ficam em st.secrets, na seção [usuarios]
    (arquivo .streamlit/secrets.toml no computador, ou Secrets no Community Cloud)."""
    usuarios = st.secrets.get("usuarios", {})
    certa = usuarios.get(usuario)
    # compare_digest compara sem "vazar" pelo tempo quantas letras acertou.
    return certa is not None and hmac.compare_digest(str(senha), str(certa))


def pedir_login():
    """Mostra a tela de login e para o app aqui até a pessoa entrar."""
    if st.session_state.get("usuario"):
        return
    _, meio, _ = st.columns([1, 2, 1])
    # A chave "login" é usada no estilo.css (.st-key-login) para o visual da tela de login.
    with meio, st.container(key="login"):
        st.image(TITULO_ESCURO if modo_escuro() else TITULO)
        with st.form("login"):
            st.markdown("<p class='instrucao-login'>Entre com seu usuário e senha</p>", unsafe_allow_html=True)
            usuario = st.text_input("Usuário")
            senha = st.text_input("Senha", type="password")
            entrar = st.form_submit_button("Entrar", width="stretch")
        if entrar:
            if senha_confere(usuario.strip(), senha):
                st.session_state.usuario = usuario.strip()
                st.rerun()
            st.error("Usuário ou senha incorretos.")
    st.stop()


# ---------- Tabela ----------
def vazio(valor):
    """None, NaN, "None", "null" e texto vazio contam como "sem informação"."""
    return valor is None or (not isinstance(valor, str) and pd.isna(valor)) or str(valor).strip().lower() in TEXTOS_VAZIOS


def em_reais(valor):
    """4.29 -> "R$ 4,29"; 1234.5 -> "R$ 1.234,50"."""
    if vazio(valor):
        return VAZIO
    texto = f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {texto}"


def em_reais_unidade(valor):
    """Preço por unidade: 2 casas, ou 3 quando for menor que R$ 1 (0.453 -> "R$ 0,453"; 0.45 -> "R$ 0,45")."""
    if vazio(valor):
        return VAZIO
    if abs(valor) >= 1:
        return em_reais(valor)
    texto = f"{valor:.3f}"
    if texto.endswith("0"):
        texto = texto[:-1]
    return "R$ " + texto.replace(".", ",")


def inteiro_br(valor):
    """11629 -> "11.629"."""
    if vazio(valor):
        return VAZIO
    return f"{int(valor):,}".replace(",", ".")


def data_br(valor):
    """"2026-10-16" -> "16/10/2026". Se não for uma data nesse formato, fica como veio."""
    if vazio(valor):
        return VAZIO
    texto = str(valor)
    if len(texto) >= 10 and texto[4] == "-" and texto[7] == "-":
        return f"{texto[8:10]}/{texto[5:7]}/{texto[0:4]}"
    return texto


def unidade_unica(colunas, linhas):
    """A unidade da coluna "unidade" quando todas as linhas têm a mesma (ex.: "comprimido"); senão, None."""
    indices = [i for i, c in enumerate(colunas) if c.lower() == "unidade"]
    unidades = {str(linha[i]).lower() for i in indices for linha in linhas if not vazio(linha[i])}
    return unidades.pop() if len(unidades) == 1 else None


def nome_coluna(coluna, unidade=None):
    """unidade: a unidade de todas as linhas (ex.: "comprimido"), para o cabeçalho do preço por unidade."""
    por_unidade = PRECO_POR_UNIDADE.match(coluna.lower())
    if por_unidade:
        inicio = TIPOS_PRECO.get(por_unidade.group(1), "Por")
        return f"{inicio} {NOMES_UNIDADES_FORMAS.get(unidade, unidade) if unidade else 'unidade'}"
    return NOMES_COLUNAS.get(coluna.lower(), coluna.replace("_", " ").capitalize())


def montar_tabela(colunas, linhas):
    """O Athena devolve tudo como texto. Aqui os números viram números (para a
    ordenação funcionar), "true"/"false" vira caixa de seleção, preços aparecem
    em reais e datas no formato brasileiro."""
    tabela = pd.DataFrame(linhas, columns=colunas)
    # Primeiro, tudo que é "sem informação" vira vazio de verdade (None), para
    # não atrapalhar a conversão dos preços em número.
    tabela = tabela.astype(object).map(lambda v: None if vazio(v) else v)
    formatos = {}
    # Se todas as linhas têm a mesma unidade, o cabeçalho do preço por unidade diz qual ("Por comprimido").
    unidade = unidade_unica(colunas, linhas)

    for coluna in tabela.columns:
        chave = coluna.lower()
        if chave in COLUNAS_TEXTO:
            continue
        valores = tabela[coluna]

        if valores.dropna().astype(str).str.lower().isin(["true", "false"]).all() and valores.notna().any():
            # Verdadeiro vira ✓, falso vira ✗ e vazio fica em branco (nunca "True", "False" ou "None").
            tabela[coluna] = valores.map(lambda v: MARCAS.get(str(v).lower(), VAZIO))
            continue

        if chave == "farmacia":
            tabela[coluna] = valores.map(lambda v: VAZIO if v is None else NOMES_FARMACIAS.get(str(v).lower(), v))
            continue

        if chave in ("unidade", "forma"):
            tabela[coluna] = valores.map(lambda v: VAZIO if v is None else NOMES_UNIDADES_FORMAS.get(str(v).lower(), v))
            continue

        if chave == "concentracao":
            # "2.5mg+12.5mg" -> "2,5mg + 12,5mg"
            tabela[coluna] = valores.map(
                lambda v: VAZIO if v is None else re.sub(r"(\d)\.(\d)", r"\1,\2", str(v)).replace("+", " + "))
            continue

        if chave in ("dt", "data_coleta") or chave.startswith("data"):
            tabela[coluna] = valores.map(data_br)
            continue

        convertida = pd.to_numeric(valores, errors="coerce")
        # Só converte se nenhum valor preenchido se perdeu na conversão.
        if convertida.notna().sum() != valores.notna().sum():
            continue
        if PRECO_POR_UNIDADE.match(chave):
            funcao = em_reais_unidade
        elif "preco" in chave or "valor" in chave:
            funcao = em_reais
        elif (convertida.dropna() % 1 == 0).all():
            funcao = inteiro_br
        else:
            funcao = None

        if convertida.isna().any():
            # O Streamlit escreve "None" em célula numérica vazia, sem respeitar o
            # formato. Então, se faltar algum valor, a coluna já vai pronta como
            # texto ("R$ 4,29" ou "-"). Nas colunas completas, o número continua
            # número e a ordenação pelo cabeçalho funciona.
            tabela[coluna] = convertida.map(funcao or (lambda v: VAZIO if vazio(v) else v))
        else:
            tabela[coluna] = convertida
            if funcao:
                formatos[coluna] = funcao

    # Texto sem informação aparece como "-", e não como "None".
    for coluna in tabela.columns:
        # (o pandas novo guarda texto como "str", e não como object: por isso o teste é "não numérica")
        if not pd.api.types.is_numeric_dtype(tabela[coluna]):
            tabela[coluna] = tabela[coluna].map(lambda v: VAZIO if vazio(v) else v)

    nomes = {coluna: nome_coluna(coluna, unidade) for coluna in tabela.columns}
    tabela = tabela.rename(columns=nomes)
    formatos = {nomes[coluna]: funcao for coluna, funcao in formatos.items()}
    # O Styler só muda o que aparece na tela: por baixo, os números continuam
    # números, então clicar no cabeçalho ordena do mais barato ao mais caro.
    estilo = tabela.style.format(formatos, na_rep=VAZIO)
    if modo_escuro():
        estilo = estilo.set_properties(**CELULAS_ESCURAS)
    return estilo


# ---------- Gráfico ----------
def em_dinheiro(coluna):
    return "preco" in coluna.lower() or "valor" in coluna.lower()


def rotulo_menor(rotulo):
    """"Preço Pix" -> "Menor preço Pix" (sem repetir se já for "Menor ...")."""
    return rotulo if rotulo.lower().startswith("menor") else f"Menor {rotulo[0].lower()}{rotulo[1:]}"


def dados_do_grafico(sugestao, colunas, linhas):
    """Confere a sugestão de gráfico e monta os pontos (uma linha por ponto:
    x, série, valor e produto). Devolve (pontos, menor) ou None se algo não
    servir: coluna que não existe, valor que não é número, data que não é data,
    menos de 2 pontos.
    Preço nunca é somado: se a mesma farmácia (ou data) aparece em várias
    linhas, fica só o menor preço de cada uma, com o produto que tem esse preço,
    e menor = True."""
    if not isinstance(sugestao, dict) or sugestao.get("tipo") not in TIPOS_GRAFICO:
        return None
    x, ys, serie = sugestao.get("x"), sugestao.get("y") or [], sugestao.get("serie")
    if x not in colunas or not ys or any(y not in colunas or y == x for y in ys):
        return None
    if len(ys) > 1 or serie not in colunas or serie == x or serie in ys:
        serie = None  # várias colunas de valores já viram várias séries
    produto = next((c for c in colunas if c.lower() in COLUNAS_PRODUTO and c not in (x, serie)), None)

    tabela = pd.DataFrame(linhas, columns=colunas).astype(object).map(lambda v: None if vazio(v) else v)
    if sugestao["tipo"] == "linha":
        # "mixed": aceita data sozinha e data com hora na mesma coluna.
        eixo_x = pd.to_datetime(tabela[x], errors="coerce", format="mixed")
        if eixo_x.isna().any():
            return None
    elif x.lower() == "farmacia":
        eixo_x = tabela[x].map(lambda v: NOMES_FARMACIAS.get(str(v).lower(), v))
    else:
        eixo_x = tabela[x].astype(str)

    pontos = []
    for y in ys:
        valores = pd.to_numeric(tabela[y], errors="coerce")
        if valores.notna().sum() != tabela[y].notna().sum():
            return None  # tem texto no meio dos valores
        if serie:
            nomes_series = tabela[serie].map(lambda v: NOMES_FARMACIAS.get(str(v).lower(), str(v)))
        else:
            nomes_series = pd.Series(nome_coluna(y), index=tabela.index)
        pontos.append(pd.DataFrame({"x": eixo_x, "serie": nomes_series, "valor": valores,
                                    "produto": tabela[produto] if produto else None}))
    pontos = pd.concat(pontos, ignore_index=True).dropna(subset=["x", "valor"])
    pontos = pontos[pontos["valor"] != SEM_ESTOQUE]

    menor = any(em_dinheiro(y) for y in ys) and pontos.duplicated(["x", "serie"]).any()
    if menor:
        # Sem isso, o gráfico empilha as barras e a altura vira a soma dos preços.
        pontos = pontos.loc[pontos.groupby(["x", "serie"], sort=False)["valor"].idxmin()]
        if not serie:
            pontos["serie"] = pontos["serie"].map(rotulo_menor)
    return (pontos, menor) if len(pontos) >= 2 else None


def montar_grafico(mensagem):
    """Gráfico de barras ou de linha com as cores da Farmazids. Devolve None
    quando não há gráfico para mostrar; nunca dá erro na tela."""
    sugestao = mensagem.get("grafico")
    try:
        dados = dados_do_grafico(sugestao, mensagem.get("colunas") or [], mensagem.get("linhas") or [])
    except Exception as erro:
        print(f"[chatbot] gráfico ignorado: {erro!r}")
        return None
    if dados is None:
        return None
    pontos, menor = dados

    cores = CORES_GRAFICO["escuro" if modo_escuro() else "claro"]
    dinheiro = any(em_dinheiro(y) for y in sugestao["y"])
    unidade = unidade_unica(mensagem.get("colunas") or [], mensagem.get("linhas") or [])
    titulo_y = (nome_coluna(sugestao["y"][0], unidade) if len(sugestao["y"]) == 1
                else ("Preço" if dinheiro else "Valor"))
    if menor:
        titulo_y = rotulo_menor(titulo_y)
    formato = "$,.2f" if dinheiro else ",.0f" if (pontos["valor"] % 1 == 0).all() else ",.2f"
    if dinheiro and any(PRECO_POR_UNIDADE.match(y.lower()) for y in sugestao["y"]) and pontos["valor"].max() < 1:
        formato = "$,.3f"  # preço por comprimido abaixo de R$ 1: 3 casas, como na tabela
    titulo_x = nome_coluna(sugestao["x"])
    uma_serie = pontos["serie"].nunique() == 1

    # Legenda em cima: embaixo, a caixa de pergunta cobre a legenda do último gráfico.
    cor = alt.Color("serie:N", title=None, scale=alt.Scale(range=cores["series"]),
                    legend=None if uma_serie else alt.Legend(orient="top"))
    valor = alt.Y("valor:Q", title=titulo_y, axis=alt.Axis(format=formato, titlePadding=14))
    dica_valor = alt.Tooltip("valor:Q", title=titulo_y, format=formato)
    # Com o menor preço, a dica mostra qual produto tem esse preço.
    dica_produto = [alt.Tooltip("produto:N", title="Produto")] if pontos["produto"].notna().any() else []
    if sugestao["tipo"] == "linha":
        # Pontos grandes: fica mais fácil parar o mouse em cima e ver o valor.
        grafico = alt.Chart(pontos).mark_line(point=alt.OverlayMarkDef(size=80), strokeWidth=2.5).encode(
            x=alt.X("x:T", title=titulo_x, axis=alt.Axis(format="%d/%m")),
            y=valor.scale(zero=False),
            color=cor,
            tooltip=[alt.Tooltip("x:T", title=titulo_x, format="%d/%m/%Y"),
                     alt.Tooltip("serie:N", title="Série"), dica_valor, *dica_produto],
        )
    else:
        muitos = pontos["x"].nunique() > 5
        grafico = alt.Chart(pontos).mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3).encode(
            x=alt.X("x:N", title=titulo_x, sort=None,
                    axis=alt.Axis(labelAngle=-35 if muitos else 0, labelLimit=160)),
            xOffset=alt.XOffset("serie:N"),
            y=valor,
            color=cor,
            tooltip=[alt.Tooltip("x:N", title=titulo_x), alt.Tooltip("serie:N", title="Série"), dica_valor,
                     *dica_produto],
        )
    return (
        grafico.properties(height=280)
        .configure(background="transparent", font="Lora", locale=IDIOMA_GRAFICO)
        .configure_axis(labelColor=cores["texto"], titleColor=cores["texto"], gridColor=cores["grade"],
                        domainColor=cores["grade"], tickColor=cores["grade"])
        .configure_legend(labelColor=cores["texto"])
        .configure_view(strokeWidth=0)
    )


# ---------- Comparação por unidade ----------
# Quando o resultado traz preço por unidade (vw_precos_unitarios), a resposta vira:
# uma frase do Bedrock, um cartão com os 3 mais baratos da apresentação principal
# (a Farmazids sempre aparece), as outras apresentações em uma linha cada e a tabela
# recolhida. Tudo é calculado aqui, a partir das colunas e linhas; o Bedrock só
# escreve a frase.
PLURAIS = {
    "comprimido": "comprimidos", "capsula": "cápsulas", "dragea": "drágeas", "sache": "sachês",
    "pastilha": "pastilhas", "ampola": "ampolas", "flaconete": "flaconetes", "supositorio": "supositórios",
    "ovulo": "óvulos", "adesivo": "adesivos", "seringa": "seringas", "dose": "doses", "unidade": "unidades",
}
# Unidade abreviada: "30 comp." nos detalhes e "/comp" nas outras apresentações.
ABREVIADAS = {"comprimido": "comp", "capsula": "cáps", "dragea": "drág", "unidade": "un"}
REMEDIO_NO_SQL = re.compile(r"like\s+'%([^%']{3,})%'", re.IGNORECASE)  # o remédio filtrado pela gerar-sql
TOP = 3  # quantas farmácias aparecem no cartão principal (fora a Farmazids)
MAXIMO_OUTRAS = 4  # linhas em "Outras apresentações"
# Formas que a pergunta pode citar, como aparecem na coluna forma.
FORMAS_NA_PERGUNTA = {
    "comprimido": r"comprimid|\bcomp\b", "capsula": r"c[áa]psula", "dragea": r"dr[áa]gea", "gotas": r"\bgotas?\b",
    "xarope": r"xarope", "solucao": r"solu[çc][ãa]o", "suspensao": r"suspens[ãa]o", "sache": r"sach[êe]",
    "creme": r"\bcreme\b", "pomada": r"pomada", "gel": r"\bgel\b", "spray": r"spray", "injetavel": r"injet[áa]vel",
}
# Nomes das farmácias como podem aparecer na frase do Bedrock (o mais longo primeiro).
FARMACIAS_NA_FRASE = re.compile(
    r"Drogaria São Paulo|Drogaria VeraCruz|Drogaria Vera Cruz|FarmaPonte|Farmaponte|VeraCruz|Vera Cruz|"
    r"São Paulo|São João|Farmazids", re.IGNORECASE)
PALAVRAS_COMUNS = {"preco", "precos", "preço", "preços", "farmacia", "farmácia", "farmacias", "farmácias",
                   "barato", "barata", "baratos", "baratas", "compare", "comparar", "comprimido", "comprimidos",
                   "qual", "quais", "onde", "mais", "menor", "maior", "custa", "valor", "valores", "gotas"}


def limpo(valor):
    """Valor pronto para mostrar: None quando é vazio ("None", "nan", "NaT"...)."""
    return None if vazio(valor) else str(valor).strip()


def numero(valor):
    try:
        x = float(str(valor).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return None if pd.isna(x) else x


def concentracao_br(texto):
    """"1000mg" -> "1 g"; "500mg/ml" -> "500 mg/ml"; "25000ui/g" -> "25.000 UI/g"."""
    def parte(p):
        m = re.match(r"^(\d+(?:\.\d+)?)(mg|mcg|ui|%|g)(/.*)?$", p)
        if not m:
            return p
        n, u, por = float(m.group(1)), m.group(2), m.group(3) or ""
        if u == "mg" and n >= 1000 and n % 1000 == 0 and not por:
            n, u = n / 1000, "g"
        valor = f"{n:,.4f}".rstrip("0").rstrip(".").replace(",", "X").replace(".", ",").replace("X", ".")
        return f"{valor}%{por}" if u == "%" else f"{valor} {'UI' if u == 'ui' else u}{por}"
    return " + ".join(parte(p) for p in str(texto).split("+"))


def unidade_br(unidade, plural=False):
    return PLURAIS.get(unidade, unidade) if plural else NOMES_UNIDADES_FORMAS.get(unidade, unidade)


def reais_juntos(valor, unidade=False):
    """"R$ 8,90" sem quebrar a linha entre o R$ e o número."""
    return (em_reais_unidade(valor) if unidade else em_reais(valor)).replace("R$ ", "R$\u00a0")


def produto_curto(texto, limite=46):
    texto = texto or ""
    return texto if len(texto) <= limite else texto[:limite - 1].rstrip() + "…"


def ler_comparacao(mensagem):
    """Lê o resultado no formato de comparação. Devolve None se não houver preço por
    unidade (aí a mensagem fica no formato de sempre)."""
    colunas, linhas = mensagem.get("colunas") or [], mensagem.get("linhas") or []
    minusculas = [c.lower() for c in colunas]
    unitarias = [c for c in minusculas if PRECO_POR_UNIDADE.match(c)]
    if "farmacia" not in minusculas or not unitarias:
        return None
    # Preço por unidade principal: o geral, senão o Pix, senão o primeiro que vier.
    ppu = next((c for c in ("preco_por_unidade", "preco_pix_por_unidade") if c in unitarias), unitarias[0])
    caixa = ppu.replace("menor_", "").replace("_por_unidade", "")
    opcoes = {
        "farmacia": ["farmacia"], "produto": ["produto", "nome", "nome_produto"], "marca": ["marca", "fabricante"],
        "ppu": [ppu], "caixa": [caixa, "preco", "preco_pix"], "quantidade": ["quantidade"],
        "unidade": ["unidade"], "concentracao": ["concentracao"], "forma": ["forma"], "coleta": ["data_coleta", "dt"],
    }
    indice = {campo: next((minusculas.index(o) for o in nomes if o in minusculas), None)
              for campo, nomes in opcoes.items()}

    def valor(linha, campo):
        i = indice[campo]
        return limpo(linha[i]) if i is not None and i < len(linha) else None

    itens = []
    for linha in linhas:
        farmacia = (valor(linha, "farmacia") or "").lower()
        itens.append({
            "farmacia": farmacia,
            "nome_farmacia": NOMES_FARMACIAS.get(farmacia, valor(linha, "farmacia") or VAZIO),
            "farmazids": farmacia == "farmazids",
            "produto": valor(linha, "produto"),
            "marca": valor(linha, "marca"),
            "ppu": numero(valor(linha, "ppu")),
            "caixa": numero(valor(linha, "caixa")),
            "quantidade": numero(valor(linha, "quantidade")),
            "unidade": (valor(linha, "unidade") or "").lower() or None,
            "concentracao": valor(linha, "concentracao"),
            "forma": (valor(linha, "forma") or "").lower() or None,
            "coleta": valor(linha, "coleta"),
        })
    com_preco = [x for x in itens if x["ppu"] is not None and x["ppu"] > 0]
    if not com_preco:
        return None

    # Apresentações (mesma concentração, forma e unidade); dentro, do mais barato ao mais caro.
    grupos = {}
    for x in com_preco:
        grupos.setdefault((x["concentracao"], x["forma"], x["unidade"]), []).append(x)
    grupos = [sorted(g, key=lambda x: x["ppu"]) for g in grupos.values()]
    pergunta = mensagem.get("pergunta") or ""
    # Combinações ("500mg+30mg") só entram no resumo se a pergunta citar a combinação.
    simples = " ".join((x["produto"] or "").lower() for x in com_preco if "+" not in (x["concentracao"] or ""))
    grupos = [g for g in grupos if "+" not in (g[0]["concentracao"] or "") or cita_combinacao(pergunta, g, simples)]
    if not grupos:
        return None
    principal = escolher_principal(grupos, pergunta)
    outras = sorted((g for g in grupos if g is not principal), key=lambda g: -len({x["farmacia"] for x in g}))

    remedio = REMEDIO_NO_SQL.search(mensagem.get("sql") or "")
    coletas = sorted(x["coleta"][:10] for x in itens if x["coleta"])
    return {
        "principal": principal,
        "outras": outras[:MAXIMO_OUTRAS],
        "remedio": remedio.group(1).strip().capitalize() if remedio else None,
        "coleta": data_br(coletas[-1]) if coletas else None,
        "total": len(itens),
    }


def cita_combinacao(pergunta, grupo, simples):
    """A pergunta cita a combinação: tem "+" ou uma palavra (de 5 letras ou mais) que
    aparece nos produtos combinados e não nos simples (ex.: "codeína" em "Paracetamol +
    Codeína"; "paracetamol" sozinho não conta, porque está nos dois)."""
    if "+" in pergunta:
        return True
    palavras = {p for p in re.findall(r"\w{5,}", pergunta.lower()) if p not in PALAVRAS_COMUNS}
    combinados = " ".join((x["produto"] or "").lower() for x in grupo)
    return any(p in combinados and p not in simples for p in palavras)


def escolher_principal(grupos, pergunta):
    """A combinação citada na pergunta; senão, a apresentação pedida (concentração ou
    forma citada); senão, a vendida pelo maior número de farmácias."""
    texto = pergunta.lower().replace(",", ".")
    pedidas = set()
    for n, u in re.findall(r"(\d+(?:\.\d+)?)\s*(mg|mcg|g|ui|ml)\b", texto):
        valor = float(n) * 1000 if u == "g" else float(n)
        pedidas.add(f"{valor:g}{'mg' if u == 'g' else u}")
    formas = {f for f, r in FORMAS_NA_PERGUNTA.items() if re.search(r, texto)}

    def nota(g):
        conc, forma = g[0]["concentracao"] or "", g[0]["forma"] or ""
        acerta_conc = bool(pedidas) and conc.split("/")[0] in pedidas
        acerta_forma = bool(formas) and forma.split(" ")[0] in formas
        # Uma combinação só sobra aqui quando a pergunta a citou: aí ela é a principal.
        return ("+" in conc, acerta_conc + acerta_forma, len({x["farmacia"] for x in g}))

    return max(grupos, key=nota)


def nome_apresentacao(grupo, remedio):
    """"Paracetamol 750 mg · comprimido"."""
    x = grupo[0]
    if not remedio and "+" in (x["concentracao"] or "") and x["produto"]:
        # Sem o nome do remédio, uma combinação viraria só números somados: usa o nome do produto.
        return produto_curto(x["produto"])
    nome = " ".join(p for p in (remedio, concentracao_br(x["concentracao"]) if x["concentracao"] else None) if p)
    return " · ".join(p for p in (nome, unidade_br(x["forma"]) if x["forma"] else None) if p)


def embalagem_curta(item):
    """"30 comp.", "20 ml", "1 seringa"; None se faltar a quantidade."""
    if item["quantidade"] is None or not item["unidade"]:
        return None
    qtd = f"{item['quantidade']:g}".replace(".", ",")
    if item["unidade"] in ABREVIADAS:
        return f"{qtd} {ABREVIADAS[item['unidade']]}."
    plural = item["quantidade"] != 1 and item["unidade"] not in ("ml", "g")
    return f"{qtd} {unidade_br(item['unidade'], plural)}"


def primeira_frase(texto):
    """Só a primeira frase do Bedrock, sem marcador de lista nem markdown."""
    texto = PALAVRAS_VAZIAS.sub(VAZIO, texto or "").replace("**", "").replace("__", "")
    linhas = [re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", l).strip() for l in texto.splitlines()]
    texto = " ".join(l for l in linhas if l)
    # Fim de frase: ponto seguido de espaço e letra maiúscula (não corta "R$ 0,34" nem "comp.").
    partes = re.split(r"(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚÂÊÔÃÕÇ])", texto, maxsplit=1)
    return partes[0].strip()


def html_frase(texto):
    """A frase com a farmácia e o primeiro preço em negrito."""
    frase = html.escape(primeira_frase(texto))
    frase = FARMACIAS_NA_FRASE.sub(lambda m: f"<b>{m.group(0)}</b>", frase, count=1)
    frase = re.sub(r"R\$\s?\d[\d.]*(?:,\d+)?", lambda m: f"<b>{m.group(0)}</b>", frase, count=1)
    return f"<p class='cmp-frase'>{frase}</p>"


def html_linha(posicao, x, melhor):
    detalhes = [x["marca"], embalagem_curta(x), reais_juntos(x["caixa"]) if x["caixa"] is not None else None]
    detalhes = " · ".join(html.escape(d) for d in detalhes if d)
    # O 1º lugar não tem percentual; empate (diferença que arredonda para 0%) também não.
    percentual = f"{(x['ppu'] - melhor) / melhor:.0%}"
    diferenca = "" if posicao == 1 or percentual == "0%" else f"<div class='cmp-dif'>+{percentual}</div>"
    classe = "cmp-linha" + (" cmp-farmazids" if x["farmazids"] else "")
    circulo = "cmp-n cmp-n1" if posicao == 1 else "cmp-n"
    dica = f" title='{html.escape(x['produto'], quote=True)}'" if x["produto"] else ""
    return (
        f"<div class='{classe}'{dica}><span class='{circulo}'>{posicao}</span>"
        f"<span class='cmp-farmacia'>{html.escape(x['nome_farmacia'])}</span>"
        f"<span class='cmp-detalhes'>{detalhes}</span>"
        f"<span class='cmp-preco'><b>{reais_juntos(x['ppu'], unidade=True)}</b>{diferenca}</span></div>"
    )


def html_comparacao(comparacao):
    """Cartão principal (os 3 mais baratos + a Farmazids) e as outras apresentações."""
    grupo, remedio = comparacao["principal"], comparacao["remedio"]
    melhor = grupo[0]["ppu"]
    unidade = unidade_br(grupo[0]["unidade"]) if grupo[0]["unidade"] else "unidade"
    linhas = [html_linha(i, x, melhor) for i, x in enumerate(grupo[:TOP], start=1)]
    # A Farmazids aparece sempre: fora do top 3, entra no fim com a posição real.
    linhas += [html_linha(i, x, melhor) for i, x in enumerate(grupo, start=1) if i > TOP and x["farmazids"]]
    cartao = (
        "<div class='cmp-cartao'><div class='cmp-titulo'>"
        f"<b>{html.escape(nome_apresentacao(grupo, remedio))}</b><span>por {html.escape(unidade)}</span></div>"
        f"{''.join(linhas)}</div>"
    )
    if not comparacao["outras"]:
        return cartao
    outras = []
    for g in comparacao["outras"]:
        x = g[0]
        abrev = ABREVIADAS.get(x["unidade"], x["unidade"] or "un")
        outras.append(
            f"<div class='cmp-outra'><span>{html.escape(nome_apresentacao(g, remedio))}</span>"
            f"<span><b>{reais_juntos(x['ppu'], unidade=True)}</b>/{html.escape(abrev)} · "
            f"{html.escape(x['nome_farmacia'])}</span></div>"
        )
    return (cartao + "<div class='cmp-cartao cmp-outras'><div class='cmp-outras-titulo'>"
            "Outras apresentações · mais barato de cada</div>" + "".join(outras) + "</div>")


def so_grupo_principal(mensagem, comparacao):
    """O gráfico mostra só a apresentação principal: preço por comprimido e preço
    por ml não dividem o mesmo eixo."""
    if not comparacao:
        return mensagem
    x = comparacao["principal"][0]
    chave = (x["concentracao"], x["forma"], x["unidade"])
    colunas = [c.lower() for c in mensagem["colunas"]]
    indices = [colunas.index(c) if c in colunas else None for c in ("concentracao", "forma", "unidade")]

    def da_linha(linha):
        valores = [limpo(linha[i]) if i is not None else None for i in indices]
        return (valores[0], (valores[1] or "").lower() or None, (valores[2] or "").lower() or None)

    return {**mensagem, "linhas": [linha for linha in mensagem["linhas"] if da_linha(linha) == chave]}


# ---------- Mensagens ----------
def limpar_texto(texto):
    """Ajustes no texto do Bedrock antes de mostrar.
    - "$": o Streamlit lê o que está entre dois cifrões como fórmula matemática,
      e "R$ 8,19; concorrente R$ 16,90" aparecia com o meio formatado (as caixas
      verdes). Com "\\$" o cifrão aparece normal.
    - "None", "nan", "null", "NaT": quando falta um valor, aparece "—"."""
    texto = PALAVRAS_VAZIAS.sub(VAZIO, texto or "")
    return texto.replace("$", "\\$")


def reais_falados(achado):
    """"R$ 0,34" -> "34 centavos"; "R$ 12,50" -> "12 reais e 50 centavos"; "R$ 1,00" -> "1 real"."""
    reais = int(achado.group(1).replace(".", ""))
    decimais = achado.group(2) or ""
    centavos = round(float("0." + decimais) * 100) if decimais else 0
    if centavos == 100:
        reais, centavos = reais + 1, 0
    partes = []
    if reais or not centavos:
        partes.append(f"{reais} {'real' if reais == 1 else 'reais'}")
    if centavos:
        partes.append(f"{centavos} {'centavo' if centavos == 1 else 'centavos'}")
    return " e ".join(partes)


def texto_para_fala(texto):
    """O que o botão "Ouvir" lê: só a frase principal da resposta, com os preços por extenso."""
    frase = primeira_frase(texto)
    frase = re.sub(r"R\$\s?(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d+))?", reais_falados, frase)
    frase = re.sub(r"[*_`#>]", "", frase.replace(VAZIO, ""))
    return re.sub(r"\s+([.,;!?])", r"\1", re.sub(r"\s+", " ", frase)).strip()


# ---------- Fala da comparação (montada aqui, com os números do ranking) ----------
ABERTURAS = ["Encontrei!", "Achei aqui.", "Vamos lá:"]
NOMES_FALADOS = {  # como cada farmácia soa melhor na voz
    "farmaponte": "Farma Ponte", "veracruz": "Drogaria Vera Cruz", "sao_joao": "São João",
    "sao_paulo": "Drogaria São Paulo", "farmazids": "Farmazids",
}
ORDINAIS = ["primeiro", "segundo", "terceiro", "quarto", "quinto", "sexto", "sétimo", "oitavo", "nono", "décimo"]
UNIDADES_FALADAS = {  # (singular, plural)
    "comprimido": ("comprimido", "comprimidos"), "capsula": ("cápsula", "cápsulas"), "dragea": ("drágea", "drágeas"),
    "ml": ("mililitro", "mililitros"), "g": ("grama", "gramas"), "dose": ("dose", "doses"), "sache": ("sachê", "sachês"),
    "pastilha": ("pastilha", "pastilhas"), "ampola": ("ampola", "ampolas"), "seringa": ("seringa", "seringas"),
    "adesivo": ("adesivo", "adesivos"), "flaconete": ("flaconete", "flaconetes"), "unidade": ("unidade", "unidades"),
}
MEDIDAS_FALADAS = {"mg": ("miligrama", "miligramas"), "mcg": ("micrograma", "microgramas"),
                   "g": ("grama", "gramas"), "ui": ("unidade internacional", "unidades internacionais"),
                   "ml": ("mililitro", "mililitros")}


def numero_falado(x):
    """12 -> "12"; 2.5 -> "2 vírgula 5"."""
    return f"{x:g}".replace(".", " vírgula ")


def preco_falado(valor):
    """0.3355 -> "34 centavos"; 10.07 -> "10 reais e 7 centavos"; 1 -> "1 real"."""
    centavos = round(valor * 100)
    if centavos == 0:
        return "menos de 1 centavo"
    reais, centavos = divmod(centavos, 100)
    partes = [f"{reais} {'real' if reais == 1 else 'reais'}"] if reais else []
    if centavos:
        partes.append(f"{centavos} {'centavo' if centavos == 1 else 'centavos'}")
    return " e ".join(partes)


def concentracao_falada(texto):
    """"750mg" -> "750 miligramas"; "1000mg" -> "1 grama"; "200mg/ml" -> "200 miligramas por mililitro"."""
    def parte(p):
        m = re.match(r"^(\d+(?:\.\d+)?)(mg|mcg|ui|%|g)(?:/(\d+(?:\.\d+)?)?(ml|g|dose|gota))?$", p)
        if not m:
            return ""
        n, u = float(m.group(1)), m.group(2)
        if u == "mg" and n >= 1000 and n % 1000 == 0:
            n, u = n / 1000, "g"
        if u == "%":
            falado = f"{numero_falado(n)} por cento"
        else:
            singular, plural = MEDIDAS_FALADAS[u]
            falado = f"{numero_falado(n)} {singular if n == 1 else plural}"
        if m.group(4):
            qtd = float(m.group(3)) if m.group(3) else 1
            singular, plural = MEDIDAS_FALADAS.get(m.group(4), (m.group(4), m.group(4) + "s"))
            falado += f" por {singular}" if qtd == 1 else f" a cada {numero_falado(qtd)} {plural}"
        return falado
    partes = [parte(p) for p in str(texto or "").split("+")]
    return " mais ".join(p for p in partes if p)


def unidade_falada(unidade, quantidade=1):
    singular, plural = UNIDADES_FALADAS.get(unidade, (unidade or "unidade", (unidade or "unidade") + "s"))
    return singular if quantidade == 1 else plural


def embalagem_falada(item):
    """"numa caixa com 20 comprimidos por 6 reais e 71 centavos" (ou "num frasco com 15 mililitros...")."""
    if item["quantidade"] is None or not item["unidade"]:
        return ""
    recipiente = {"ml": "num frasco", "g": "numa embalagem"}.get(item["unidade"], "numa caixa")
    texto = f"{recipiente} com {numero_falado(item['quantidade'])} {unidade_falada(item['unidade'], item['quantidade'])}"
    if item["caixa"] is not None:
        texto += f" por {preco_falado(item['caixa'])}"
    return texto


def nome_falado(item):
    return NOMES_FALADOS.get(item["farmacia"], item["nome_farmacia"])


def fala_da_comparacao(comparacao, chave):
    """Resumo falado da comparação principal. Devolve (texto, farmácia a destacar)."""
    grupo = comparacao["principal"]
    melhor = grupo[0]
    unidade = unidade_falada(melhor["unidade"])
    embalagem = embalagem_falada(melhor)
    detalhe = f"{preco_falado(melhor['ppu'])} por {unidade}" + (f", {embalagem}" if embalagem else "")
    frases = []
    if melhor["farmazids"]:
        frases.append(f"Boa notícia: a Farmazids é a mais barata, com {detalhe}.")
    else:
        abertura = ABERTURAS[(chave if isinstance(chave, int) else 0) % len(ABERTURAS)]
        remedio = (comparacao["remedio"] or "").lower()
        feminino = remedio.endswith("a")  # a dipirona, o paracetamol
        produto = " ".join(p for p in (remedio, concentracao_falada(melhor["concentracao"])) if p)
        if produto:
            artigo = "a" if feminino else "o"
            artigo = artigo if abertura.endswith(":") else artigo.upper()  # "Vamos lá: o paracetamol"
            frases.append(f"{abertura} {artigo} {produto} está mais "
                          f"{'barata' if feminino else 'barato'} na {nome_falado(melhor)}: {detalhe}.")
        else:
            frases.append(f"{abertura} O mais barato está na {nome_falado(melhor)}: {detalhe}.")
    seguintes = [f"a {nome_falado(x)}, com {preco_falado(x['ppu'])}" for x in grupo[1:TOP]]
    if len(seguintes) == 2:
        frases.append(f"Logo depois vêm {seguintes[0]}, e {seguintes[1]}.")
    elif seguintes:
        frases.append(f"Logo depois vem {seguintes[0]}.")
    posicao = next((i for i, x in enumerate(grupo, start=1) if x["farmazids"]), None)
    if posicao and posicao > TOP:
        lugar = ORDINAIS[posicao - 1] if posicao <= len(ORDINAIS) else f"{posicao}º"
        frases.append(f"A Farmazids aparece em {lugar} lugar, com {preco_falado(grupo[posicao - 1]['ppu'])}.")
    return " ".join(frases), nome_falado(melhor)


def fala_da_mensagem(mensagem, comparacao, chave):
    """(texto, destaque) que o "Ouvir" lê: na comparação, o resumo da tela; senão, o campo
    "fala" da redigir-resposta; sem ele, a primeira frase da resposta."""
    if comparacao:
        return fala_da_comparacao(comparacao, chave)
    if limpo(mensagem.get("fala")):
        return texto_para_fala_completo(mensagem["fala"]), None
    return texto_para_fala(mensagem["texto"]), None


def texto_para_fala_completo(texto):
    """O campo "fala" inteiro (já vem escrito para o ouvido), só limpo de símbolos."""
    texto = PALAVRAS_VAZIAS.sub("", texto or "").replace("**", "")
    texto = re.sub(r"R\$\s?(\d{1,3}(?:\.\d{3})+|\d+)(?:,(\d+))?", reais_falados, texto)
    texto = re.sub(r"[*_`#>]", "", texto)
    return re.sub(r"\s+([.,;!?])", r"\1", re.sub(r"\s+", " ", texto)).strip()


# ---------- Áudio do Amazon Polly ----------
def ssml_da_fala(texto, destaque=None):
    """Pausa curta entre as frases, velocidade levemente acima do normal e ênfase leve
    (um pouco mais de volume) no nome da farmácia mais barata."""
    frases = [html.escape(f, quote=False) for f in re.split(r"(?<=[.!?:])\s+", texto) if f.strip()]
    corpo = f'<break time="{POLLY_PAUSA}"/>'.join(frases)
    if destaque:
        nome = html.escape(destaque, quote=False)
        corpo = corpo.replace(nome, f'<prosody volume="+3dB">{nome}</prosody>', 1)
    return f'<speak><prosody rate="{POLLY_VELOCIDADE}">{corpo}</prosody></speak>'


@st.cache_data(show_spinner=False, max_entries=500)
def _audio_polly(ssml):
    """Guardado pelo texto: o Polly não é chamado duas vezes para a mesma fala. Uma falha
    levanta exceção, e exceção não fica no cache (a próxima vez tenta de novo)."""
    return sintetizar(ssml, POLLY_VOZ)


@st.cache_resource
def _estado_polly():
    """Depois de uma falha, a voz do navegador é usada por um tempo. Fica no cache_resource
    porque o app.py roda de novo a cada interação (uma variável comum voltaria a zero)."""
    return {"parado_ate": 0.0}


def audio_da_fala(texto, destaque=None):
    """(bytes, tipo) do Polly, ou None para a tela usar a voz do navegador. Nunca dá erro na tela."""
    estado = _estado_polly()
    if not texto or time.time() < estado["parado_ate"]:
        return None
    try:
        return _audio_polly(ssml_da_fala(texto, destaque))
    except Exception as erro:
        try:  # o destaque usa um recurso de SSML; sem ele, a voz ainda sai do Polly
            if destaque:
                return _audio_polly(ssml_da_fala(texto))
        except Exception:
            pass
        print(f"[chatbot] Polly falhou, usando a voz do navegador por {POLLY_ESPERA_FALHA // 60} min: {erro!r}")
        estado["parado_ate"] = time.time() + POLLY_ESPERA_FALHA
        return None


def audio_sob_demanda(chave, fala, destaque):
    """O Polly só é chamado quando a fala vai ser ouvida. O "Ouvir" (ou a leitura automática
    de uma pergunta feita por voz) clica num botão escondido desta resposta; o app roda de
    novo, e aqui o áudio é gerado e posto num player escondido, que o assets/voz.js toca.
    Depois disso, a resposta continua com o áudio (do cache, sem chamar o Polly de novo).
    Devolve a origem da voz: "polly" (áudio pronto), "sob-demanda" (o Polly será chamado
    no clique) ou "navegador" (o Polly falhou ou está em pausa depois de uma falha)."""
    com_audio = st.session_state.setdefault("respostas_com_audio", set())
    if st.button("Gerar áudio", key=f"pedir-audio-{chave}"):  # escondido pelo CSS
        com_audio.add(chave)
    if chave not in com_audio:
        return "navegador" if time.time() < _estado_polly()["parado_ate"] else "sob-demanda"
    audio = audio_da_fala(fala, destaque)
    if not audio:
        return "navegador"
    with st.container(key=f"audio-{chave}"):
        st.audio(audio[0], format=audio[1])
    return "polly"


def rodape(mensagem, coleta=None, chave=None, fala="", origem=""):
    """Hora da mensagem (ou, na comparação, a data da coleta), quanto tempo levou e,
    nas respostas, o botão "Ouvir" (o assets/voz.js cuida dele). origem diz de onde vem
    a voz ("polly", "sob-demanda" ou "navegador"), para a dica do botão e para conferir."""
    texto = f"Coleta de {coleta}" if coleta else mensagem["hora"]
    if mensagem.get("do_cache"):
        texto += " · resposta salva"
    elif mensagem.get("duracao") is not None:
        texto += f" · respondido em {mensagem['duracao']:.0f} s"
    if chave is not None and fala:
        dica = "Voz do navegador" if origem == "navegador" else f"Voz: Amazon Polly ({POLLY_VOZ})"
        texto += (f" <button type='button' class='voz-ouvir' data-id='{chave}' data-origem='{origem}' "
                  f"title='{html.escape(dica, quote=True)}' "
                  f"data-fala='{html.escape(fala, quote=True)}'>🔊 Ouvir</button>")
    return f"<div class='hora{' cmp-rodape' if coleta else ''}'>{texto}</div>"


def mostrar(mensagem, chave):
    """Desenha uma mensagem. A chave do container vira uma classe CSS
    (st-key-...-user ou st-key-...-assistant), usada no estilo dos balões."""
    papel = mensagem["papel"]
    avatar = LOGO if papel == "assistant" else ":material/person:"
    with st.container(key=f"msg-{chave}-{papel}"):
        with st.chat_message(papel, avatar=avatar):
            comparacao = ler_comparacao(mensagem) if papel == "assistant" and mensagem.get("colunas") else None
            if comparacao:
                st.html(html_frase(mensagem["texto"]) + html_comparacao(comparacao))
            else:
                st.markdown(limpar_texto(mensagem["texto"]))
            if mensagem.get("colunas"):
                tabela = montar_tabela(mensagem["colunas"], mensagem["linhas"])
                if comparacao:
                    # Na comparação, a tabela completa fica recolhida.
                    total = comparacao["total"]
                    with st.expander(f"Ver tabela completa ({total} produto{'s' if total != 1 else ''})",
                                     key=f"tabela-{chave}"):
                        st.dataframe(tabela, hide_index=True, width="stretch", column_config=LARGURAS)
                else:
                    st.dataframe(tabela, hide_index=True, width="stretch", column_config=LARGURAS)
                # Gráfico só quando a pergunta pediu; aí ele já aparece aberto.
                grafico = montar_grafico(so_grupo_principal(mensagem, comparacao)) \
                    if mensagem.get("pediu_grafico") else None
                if grafico is not None:
                    with st.expander("Ver gráfico", expanded=True,
                                     icon=":material/bar_chart:", key=f"grafico-{chave}"):
                        st.altair_chart(grafico, theme=None, width="stretch")
            # "Ouvir" só nas respostas (não na pergunta nem nas boas-vindas).
            fala, origem = "", ""
            if papel == "assistant" and chave != "inicio":
                fala, destaque = fala_da_mensagem(mensagem, comparacao, chave)
                origem = audio_sob_demanda(chave, fala, destaque)
            st.html(rodape(mensagem, comparacao and comparacao["coleta"], chave, fala, origem))


def texto_espera(segundos):
    pontos = "<div class='digitando'><span></span><span></span><span></span></div>"
    texto = f"Consultando os dados... <span class='cronometro'>{segundos} s</span>"
    if segundos >= SEGUNDOS_AVISO_DEMORA:
        texto += ("<div class='aviso-demora'>Algumas perguntas levam mais tempo, "
                  "quando a consulta precisa ser corrigida antes de rodar.</div>")
    return pontos + texto


def montar_historico(mensagens):
    """Últimas trocas que deram certo, para o backend entender perguntas de
    continuação ("e na São João?"). Vai a pergunta, o começo da resposta e o
    SQL usado; a tabela não vai. As respostas teóricas ("para que serve a
    dipirona?") entram com o SQL vazio, para "e quanto custa?" saber de qual
    remédio se trata. Erros e "não encontrei nos dados" ficam de fora.
    O SQL fica guardado nas mensagens só para isso: nunca aparece na tela."""
    trocas = []
    for anterior, mensagem in zip(mensagens, mensagens[1:]):
        if (anterior["papel"] == "user" and mensagem["papel"] == "assistant" and mensagem.get("ok")
                and (mensagem.get("sql") or mensagem.get("tipo") == "teorica")):
            trocas.append({
                "pergunta": anterior["texto"],
                "resposta": " ".join(mensagem["texto"].split())[:CARACTERES_RESPOSTA],
                "sql": (mensagem.get("sql") or "")[:CARACTERES_SQL],
            })
    return trocas[-TROCAS_NO_HISTORICO:]


def responder(pergunta, historico):
    """Chama o backend numa thread separada e, enquanto isso, atualiza o
    cronômetro do balão de espera a cada meio segundo."""
    espera = st.empty()
    with espera.container(key="msg-espera-assistant"):
        with st.chat_message("assistant", avatar=LOGO):
            area_texto = st.empty()

    inicio = time.perf_counter()
    with ThreadPoolExecutor(max_workers=1) as executor:
        tarefa = executor.submit(perguntar, pergunta, historico)
        while not tarefa.done():
            area_texto.markdown(texto_espera(int(time.perf_counter() - inicio)), unsafe_allow_html=True)
            time.sleep(0.5)
        resultado = tarefa.result()
    espera.empty()
    return resultado


# ---------- Página ----------
st.set_page_config(page_title="Farmazids | Chatbot", page_icon=LOGO, layout="wide")

if "tema" not in st.session_state:
    st.session_state.tema = "claro"  # "claro" ou "escuro", trocado pelo botão do cabeçalho
if "mensagens" not in st.session_state:
    st.session_state.mensagens = []
if "hora_inicio" not in st.session_state:
    st.session_state.hora_inicio = agora()
if "pendente" not in st.session_state:
    st.session_state.pendente = None  # pergunta esperando resposta

aplicar_estilo()
pedir_login()
instalar_voz()

# ---------- Cabeçalho (fica fixo no topo: ver .st-key-cabecalho no estilo.css) ----------
with st.container(key="cabecalho"):
    # Botão claro/escuro no canto esquerdo (ver .st-key-botao-tema no estilo.css).
    # Fica bloqueado enquanto uma resposta está a caminho: trocar o tema
    # redesenha a tela, e isso interromperia a resposta.
    st.button(
        "Claro" if modo_escuro() else "Escuro",
        key="botao-tema",
        icon=":material/light_mode:" if modo_escuro() else ":material/dark_mode:",
        help="Mudar para o modo claro" if modo_escuro() else "Mudar para o modo escuro",
        on_click=trocar_tema,
        disabled=st.session_state.pendente is not None,
    )
    # O tamanho do letreiro vem do estilo.css (.st-key-cabecalho img): menor no
    # celular que no computador. O lema fica à direita e some em telas estreitas.
    st.image(TITULO_ESCURO if modo_escuro() else TITULO)
    st.markdown("<div class='lema'>TRADIÇÃO<br>QUE GERA<br>CONHECIMENTO</div>", unsafe_allow_html=True)
    st.markdown(
        "<p class='subtitulo'>Assistente de dados para consulta de preços, "
        "concorrentes, vendas e medicamentos.</p>",
        unsafe_allow_html=True,
    )
    if MODO_SIMULADO:
        st.markdown(
            "<p class='aviso-simulado'>Modo simulado: respostas de teste, sem consultar a AWS.</p>",
            unsafe_allow_html=True,
        )

# ---------- Conversa ----------
# Redesenha a conversa inteira a cada interação (é assim que o Streamlit funciona).
mostrar({"papel": "assistant", "texto": BOAS_VINDAS, "hora": st.session_state.hora_inicio}, "inicio")
for i, mensagem in enumerate(st.session_state.mensagens):
    mostrar(mensagem, i)

# A caixa fica bloqueada enquanto uma resposta está a caminho. Sem isso, mandar
# outra pergunta no meio interrompe a primeira, e a resposta dela se perde.
nova = st.chat_input(
    "Aguarde a resposta..." if st.session_state.pendente else "Digite sua pergunta...",
    disabled=st.session_state.pendente is not None,
)

if nova:
    st.session_state.mensagens.append({"papel": "user", "texto": nova, "hora": agora()})
    st.session_state.pendente = nova
    st.rerun()  # redesenha já com a pergunta na tela e a caixa bloqueada

if st.session_state.pendente:
    try:
        # A última mensagem é a própria pergunta pendente: fica fora do histórico.
        historico = montar_historico(st.session_state.mensagens[:-1])
        resultado = responder(st.session_state.pendente, historico)
    except Exception as erro:  # nunca deixar a caixa bloqueada para sempre
        print(f"[chatbot] erro inesperado: {erro!r}")
        resultado = {"ok": False, "resposta": "Algo deu errado ao responder. Tente de novo em instantes.",
                     "sql": None, "colunas": [], "linhas": [], "duracao": None, "do_cache": False}
    finally:
        st.session_state.pendente = None

    st.session_state.mensagens.append({
        "papel": "assistant",
        "texto": resultado["resposta"],
        "ok": resultado["ok"],
        "sql": resultado["sql"],  # só para o histórico; mostrar() não exibe
        "tipo": resultado.get("tipo"),  # "dados", "teorica" ou "sem_dados"
        "grafico": resultado.get("grafico"),  # sugestão de gráfico; a tela confere antes de desenhar
        # Confere aqui também, pela palavra "gráfico" na pergunta: sem ela, nada de gráfico.
        "pediu_grafico": bool(PEDIU_GRAFICO.search(st.session_state.mensagens[-1]["texto"])),
        "pergunta": st.session_state.mensagens[-1]["texto"],  # a comparação usa a apresentação pedida
        "fala": resultado.get("fala"),  # a resposta escrita para ser ouvida (redigir-resposta)
        "colunas": resultado["colunas"],
        "linhas": resultado["linhas"],
        "duracao": resultado.get("duracao"),
        "do_cache": resultado.get("do_cache", False),
        "hora": agora(),
    })
    st.rerun()  # desbloqueia a caixa e mostra a resposta
