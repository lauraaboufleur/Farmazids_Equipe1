"""Tela do chatbot da Farmazids.

Rodar a partir da raiz do repositório (é lá que fica o .streamlit/config.toml
com as cores e fontes da marca):
    streamlit run src/chatbot/app.py

O visual segue o template aprovado: o estilo fica em assets/estilo.css e as
imagens em assets/. Este arquivo só monta a tela e conversa com o backend.
"""

import base64
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from cliente_chatbot import MODO_SIMULADO, perguntar

ASSETS = Path(__file__).parent / "assets"
LOGO = str(ASSETS / "logo_farmazids.png")                    # símbolo redondo
TITULO = str(ASSETS / "titulo_farmazids.png")                # letreiro roxo e dourado (fundo claro)
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
    "maior_preco": "Maior preço",
    "em_estoque": "Estoque",
    "data_coleta": "Coleta",
    "dt": "Coleta",
    "desconto": "Desconto",
    "quantidade": "Quantidade",
}

# Larguras fixas para a tabela caber no balão sem rolar para o lado. O nome do
# produto pode ficar cortado; arrastando a borda do cabeçalho, a coluna aumenta.
LARGURAS = {"Produto": st.column_config.Column(width=300),
            "Estoque": st.column_config.Column(width=74),
            "Coleta": st.column_config.Column(width=100)}

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

# Depois desse tempo, o balão de espera explica por que está demorando.
SEGUNDOS_AVISO_DEMORA = 15


def agora():
    return datetime.now(FUSO).strftime("%H:%M")


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
    st.html(f"<style>{variaveis}\n{css}</style>")


# ---------- Tabela ----------
def em_reais(valor):
    """4.29 -> "R$ 4,29"; 1234.5 -> "R$ 1.234,50"."""
    if pd.isna(valor):
        return ""
    texto = f"{valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {texto}"


def inteiro_br(valor):
    """11629 -> "11.629"."""
    if pd.isna(valor):
        return ""
    return f"{int(valor):,}".replace(",", ".")


def data_br(valor):
    """"2026-10-16" -> "16/10/2026". Se não for uma data nesse formato, fica como veio."""
    if valor is None or pd.isna(valor):
        return ""
    texto = str(valor)
    if len(texto) >= 10 and texto[4] == "-" and texto[7] == "-":
        return f"{texto[8:10]}/{texto[5:7]}/{texto[0:4]}"
    return texto


def nome_coluna(coluna):
    return NOMES_COLUNAS.get(coluna.lower(), coluna.replace("_", " ").capitalize())


def montar_tabela(colunas, linhas):
    """O Athena devolve tudo como texto. Aqui os números viram números (para a
    ordenação funcionar), "true"/"false" vira caixa de seleção, preços aparecem
    em reais e datas no formato brasileiro."""
    tabela = pd.DataFrame(linhas, columns=colunas)
    formatos = {}

    for coluna in tabela.columns:
        chave = coluna.lower()
        if chave in COLUNAS_TEXTO:
            continue
        valores = tabela[coluna]

        if valores.dropna().astype(str).str.lower().isin(["true", "false"]).all() and valores.notna().any():
            tabela[coluna] = valores.astype(str).str.lower().map({"true": True, "false": False})
            continue

        if chave == "farmacia":
            tabela[coluna] = valores.map(lambda v: NOMES_FARMACIAS.get(str(v).lower(), v))
            continue

        if chave in ("dt", "data_coleta") or chave.startswith("data"):
            tabela[coluna] = valores.map(data_br)
            continue

        convertida = pd.to_numeric(valores, errors="coerce")
        # Só converte se nenhum valor preenchido se perdeu na conversão.
        if convertida.notna().sum() != valores.notna().sum():
            continue
        tabela[coluna] = convertida
        if "preco" in chave or "valor" in chave:
            formatos[coluna] = em_reais
        elif (convertida.dropna() % 1 == 0).all():
            formatos[coluna] = inteiro_br

    nomes = {coluna: nome_coluna(coluna) for coluna in tabela.columns}
    tabela = tabela.rename(columns=nomes)
    formatos = {nomes[coluna]: funcao for coluna, funcao in formatos.items()}
    # O Styler só muda o que aparece na tela: por baixo, os números continuam
    # números, então clicar no cabeçalho ordena do mais barato ao mais caro.
    return tabela.style.format(formatos) if formatos else tabela


# ---------- Mensagens ----------
def rodape(mensagem):
    """Hora da mensagem e, nas respostas, quanto tempo levou."""
    texto = mensagem["hora"]
    if mensagem.get("do_cache"):
        texto += " · resposta salva"
    elif mensagem.get("duracao") is not None:
        texto += f" · respondido em {mensagem['duracao']:.0f} s"
    return f"<div class='hora'>{texto}</div>"


def mostrar(mensagem, chave):
    """Desenha uma mensagem. A chave do container vira uma classe CSS
    (st-key-...-user ou st-key-...-assistant), usada no estilo dos balões."""
    papel = mensagem["papel"]
    avatar = LOGO if papel == "assistant" else ":material/person:"
    with st.container(key=f"msg-{chave}-{papel}"):
        with st.chat_message(papel, avatar=avatar):
            st.markdown(mensagem["texto"])
            if mensagem.get("colunas"):
                st.dataframe(
                    montar_tabela(mensagem["colunas"], mensagem["linhas"]),
                    hide_index=True,
                    width="stretch",
                    column_config=LARGURAS,
                )
            if mensagem.get("sql"):
                with st.expander("Ver o SQL usado"):
                    st.code(mensagem["sql"], language="sql")
            st.markdown(rodape(mensagem), unsafe_allow_html=True)


def texto_espera(segundos):
    pontos = "<div class='digitando'><span></span><span></span><span></span></div>"
    texto = f"Consultando os dados... <span class='cronometro'>{segundos} s</span>"
    if segundos >= SEGUNDOS_AVISO_DEMORA:
        texto += ("<div class='aviso-demora'>Algumas perguntas levam mais tempo, "
                  "quando a consulta precisa ser corrigida antes de rodar.</div>")
    return pontos + texto


def responder(pergunta):
    """Chama o backend numa thread separada e, enquanto isso, atualiza o
    cronômetro do balão de espera a cada meio segundo."""
    espera = st.empty()
    with espera.container(key="msg-espera-assistant"):
        with st.chat_message("assistant", avatar=LOGO):
            area_texto = st.empty()

    inicio = time.perf_counter()
    with ThreadPoolExecutor(max_workers=1) as executor:
        tarefa = executor.submit(perguntar, pergunta)
        while not tarefa.done():
            area_texto.markdown(texto_espera(int(time.perf_counter() - inicio)), unsafe_allow_html=True)
            time.sleep(0.5)
        resultado = tarefa.result()
    espera.empty()
    return resultado


# ---------- Página ----------
st.set_page_config(page_title="Farmazids | Chatbot", page_icon=LOGO, layout="wide")
aplicar_estilo()

if "mensagens" not in st.session_state:
    st.session_state.mensagens = []
if "hora_inicio" not in st.session_state:
    st.session_state.hora_inicio = agora()
if "pendente" not in st.session_state:
    st.session_state.pendente = None  # pergunta esperando resposta

# ---------- Cabeçalho (fica fixo no topo: ver .st-key-cabecalho no estilo.css) ----------
with st.container(key="cabecalho"):
    _, centro, direita = st.columns([1, 1.8, 1])  # coluna do meio define o tamanho do letreiro
    with centro:
        st.image(TITULO, width="stretch")
    with direita:
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
        resultado = responder(st.session_state.pendente)
    except Exception as erro:  # nunca deixar a caixa bloqueada para sempre
        print(f"[chatbot] erro inesperado: {erro!r}")
        resultado = {"ok": False, "resposta": "Algo deu errado ao responder. Tente de novo em instantes.",
                     "sql": None, "colunas": [], "linhas": [], "duracao": None, "do_cache": False}
    finally:
        st.session_state.pendente = None

    st.session_state.mensagens.append({
        "papel": "assistant",
        "texto": resultado["resposta"],
        "sql": resultado["sql"],
        "colunas": resultado["colunas"],
        "linhas": resultado["linhas"],
        "duracao": resultado.get("duracao"),
        "do_cache": resultado.get("do_cache", False),
        "hora": agora(),
    })
    st.rerun()  # desbloqueia a caixa e mostra a resposta
