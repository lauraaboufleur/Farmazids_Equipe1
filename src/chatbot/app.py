"""Tela do chatbot da Farmazids.

Rodar a partir da raiz do repositório (é lá que fica o .streamlit/config.toml
com as cores e fontes da marca):
    streamlit run src/chatbot/app.py

O visual segue o template aprovado: o estilo fica em assets/estilo.css e as
imagens em assets/. Este arquivo só monta a tela e conversa com o backend.
"""

import base64
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from cliente_chatbot import MODO_SIMULADO, perguntar

ASSETS = Path(__file__).parent / "assets"
LOGO = str(ASSETS / "logo_farmazids.png")                    # símbolo redondo
TITULO = str(ASSETS / "titulo_farmazids.png")                # letreiro roxo e dourado (fundo claro)
TITULO_CLARO = str(ASSETS / "titulo_farmazids_claro.png")    # letreiro creme e dourado (fundo roxo)
FUSO = ZoneInfo("America/Sao_Paulo")

# Colunas que parecem número, mas são códigos (não podem perder zeros à esquerda).
COLUNAS_TEXTO = {"ean", "sku", "id_venda"}

# (ícone, pergunta) dos cartões de exemplo da barra lateral.
EXEMPLOS = [
    (":material/chat:", "Qual o preço da dipirona em cada farmácia?"),
    (":material/bar_chart:", "Quais os 10 medicamentos mais vendidos pela Farmazids?"),
    (":material/medication:", "Em quais produtos a Farmazids está mais cara que a São João?"),
]

BOAS_VINDAS = (
    "**Olá!** Sou o **assistente de dados da Farmazids.**\n\n"
    "Posso te ajudar a consultar preços, comparar com concorrentes, analisar "
    "vendas e obter informações sobre nossos medicamentos.\n\n"
    "Faça uma pergunta ou escolha um exemplo ao lado."
)

ICONE_CHAT = (
    '<svg viewBox="0 0 24 24" fill="none" stroke-width="1.6" stroke-linecap="round" '
    'stroke-linejoin="round"><path d="M4 5h16v11H9l-5 4z"/>'
    '<path d="M8.5 10.5h.01M12 10.5h.01M15.5 10.5h.01" stroke-width="2.4"/></svg>'
)


def agora():
    return datetime.now(FUSO).strftime("%H:%M")


def svg_como_url(nome):
    dados = base64.b64encode((ASSETS / "decoracao" / nome).read_bytes()).decode()
    return f'url("data:image/svg+xml;base64,{dados}")'


def aplicar_estilo():
    variaveis = (
        ":root {"
        f"--ramo: {svg_como_url('ramo.svg')};"
        f"--nuvens: {svg_como_url('nuvens.svg')};"
        f"--ondas: {svg_como_url('ondas.svg')};"
        f"--pessoa: {svg_como_url('pessoa.svg')};"
        f"--aviao: {svg_como_url('aviao.svg')};"
        "}"
    )
    css = (ASSETS / "estilo.css").read_text(encoding="utf-8")
    st.html(f"<style>{variaveis}\n{css}</style>")


def escolher_exemplo(texto):
    st.session_state.pergunta_exemplo = texto


def limpar_conversa():
    st.session_state.mensagens = []


def montar_tabela(colunas, linhas):
    """O Athena devolve tudo como texto; aqui os números viram números."""
    tabela = pd.DataFrame(linhas, columns=colunas)
    for coluna in tabela.columns:
        if coluna.lower() in COLUNAS_TEXTO:
            continue
        convertida = pd.to_numeric(tabela[coluna], errors="coerce")
        # Só converte se nenhum valor preenchido se perdeu na conversão.
        if convertida.notna().sum() == tabela[coluna].notna().sum():
            tabela[coluna] = convertida
    return tabela


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
                )
            if mensagem.get("sql"):
                with st.expander("Ver o SQL usado"):
                    st.code(mensagem["sql"], language="sql")
            st.markdown(f"<div class='hora'>{mensagem['hora']}</div>", unsafe_allow_html=True)


# ---------- Página ----------
st.set_page_config(page_title="Farmazids | Chatbot", page_icon=LOGO, layout="wide")
aplicar_estilo()

if "mensagens" not in st.session_state:
    st.session_state.mensagens = []
if "hora_inicio" not in st.session_state:
    st.session_state.hora_inicio = agora()

# ---------- Barra lateral ----------
with st.sidebar:
    with st.container(key="marca"):
        st.image(LOGO, width=190)
        st.image(TITULO_CLARO, width=270)
    st.markdown(f"<div class='menu-item'>{ICONE_CHAT}Assistente de dados</div>", unsafe_allow_html=True)
    st.markdown("<p class='rotulo-exemplos'>EXEMPLOS DE PERGUNTAS</p>", unsafe_allow_html=True)
    for i, (icone, texto) in enumerate(EXEMPLOS):
        st.button(texto, key=f"exemplo-{i}", icon=icone, width="stretch",
                  on_click=escolher_exemplo, args=(texto,))
    st.button("Limpar conversa", key="limpar", icon=":material/refresh:", on_click=limpar_conversa)

# ---------- Cabeçalho ----------
_, centro, direita = st.columns([1, 2.2, 1])
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

pergunta = st.chat_input("Digite sua pergunta...")
pergunta = pergunta or st.session_state.pop("pergunta_exemplo", None)

if pergunta:
    mensagem_usuario = {"papel": "user", "texto": pergunta, "hora": agora()}
    st.session_state.mensagens.append(mensagem_usuario)
    mostrar(mensagem_usuario, len(st.session_state.mensagens) - 1)

    # Balão de "digitando..." enquanto o backend responde.
    espera = st.empty()
    with espera.container(key="msg-espera-assistant"):
        with st.chat_message("assistant", avatar=LOGO):
            st.markdown(
                "<div class='digitando'><span></span><span></span><span></span></div>"
                "Consultando os dados...",
                unsafe_allow_html=True,
            )
    resultado = perguntar(pergunta)
    espera.empty()

    mensagem_assistente = {
        "papel": "assistant",
        "texto": resultado["resposta"],
        "sql": resultado["sql"],
        "colunas": resultado["colunas"],
        "linhas": resultado["linhas"],
        "hora": agora(),
    }
    st.session_state.mensagens.append(mensagem_assistente)
    mostrar(mensagem_assistente, len(st.session_state.mensagens) - 1)
