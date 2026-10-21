import re
import sys
import unicodedata
from datetime import datetime

import pandas as pd

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", None)
pd.set_option("display.max_colwidth", 60)

CAMPOS_PRECO = ["preco_sem_desconto", "preco_cartao", "preco_pix"]
LIMITE_DESCONTO_ALTO = 80
# Farmácias em que a marca vazia é preenchida pelo nome do produto (ver marca_pelo_nome).
FARMACIAS_MARCA_PELO_NOME = {"farmaponte"}
# Só vale como marca pelo nome a que aparece em pelo menos tantos produtos do cadastro
# (marca de um produto só costuma ser nome de linha, como "Prevent").
MINIMO_PRODUTOS_MARCA = 3
# Marcas do cadastro que não valem pelo nome (escritas sem acento, em minúsculas).
MARCAS_IGNORADAS_PELO_NOME = {"prevent"}
# Outros jeitos de escrever uma marca do cadastro no nome: grafia no nome -> marca do cadastro.
MARCAS_EQUIVALENTES = {"ache biosintetica": "Biosintética - Aché"}


def ler_raw(caminho):
    return pd.read_json(caminho, lines=True, dtype={"sku": str, "ean": str})


def converter_tipos(df):
    df = df.copy()
    for campo in CAMPOS_PRECO:
        df[campo] = pd.to_numeric(df[campo], errors="coerce")
    df["data_coleta"] = pd.to_datetime(df["data_coleta"])
    # "boolean" (e não bool) aceita True, False e vazio: a página nem sempre informa o estoque
    df["em_estoque"] = df["em_estoque"].astype("boolean")
    return df


def calcular_descontos(df):
    df = df.copy()
    df["desconto_valor"] = (df["preco_sem_desconto"] - df["preco_cartao"]).round(2)
    df["desconto_percentual"] = (df["desconto_valor"] / df["preco_sem_desconto"] * 100).round(2)
    df = df.drop(columns=["desconto"])
    return df


def limpar_textos(df):
    df = df.copy()
    for campo in ["nome", "marca"]:
        df[campo] = (
            df[campo]
            .str.replace("\ufeff", "", regex=False)
            .str.replace(r"\s+", " ", regex=True)
            .str.strip()
        )
    df["marca"] = df["marca"].replace({"None": pd.NA, "": pd.NA})
    return df


def sem_acento(texto):
    """Minúsculas, sem acento e com pontuação virando espaço ("Prati-Donaduzzi" -> "prati donaduzzi")."""
    texto = unicodedata.normalize("NFD", str(texto).lower())
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", texto).split())


def marca_pelo_nome(df):
    """Preenche a marca vazia quando o nome do produto contém, como palavra inteira, uma marca
    que já aparece em pelo menos MINIMO_PRODUTOS_MARCA produtos do próprio cadastro da farmácia
    (comparando sem acento e sem diferença de maiúsculas), ou uma grafia de MARCAS_EQUIVALENTES.
    Se o nome casar com mais de uma marca, fica vazia; uma marca escrita dentro de outra que
    também casou ("Aché" dentro de "Biosintética - Aché") conta como a maior. Nunca usa marca de fora."""
    df = df.copy()
    contagem = df["marca"].dropna().value_counts()
    marcas = {}  # grafia sem acento -> marca como está no cadastro
    for marca, produtos in contagem.items():
        chave = sem_acento(marca)
        if chave and produtos >= MINIMO_PRODUTOS_MARCA and chave not in MARCAS_IGNORADAS_PELO_NOME:
            marcas.setdefault(chave, marca)
    for grafia, marca in MARCAS_EQUIVALENTES.items():
        if marca in contagem.index:
            marcas[grafia] = marca
    padroes = {chave: re.compile(rf"\b{re.escape(chave)}\b") for chave in marcas}

    def achar(nome):
        texto = sem_acento(nome)
        casadas = [chave for chave, padrao in padroes.items() if padrao.search(texto)]
        # "ache" dentro de "ache biosintetica": fica só a maior.
        casadas = [c for c in casadas if not any(c != o and re.search(rf"\b{re.escape(c)}\b", o) for o in casadas)]
        encontradas = {marcas[c] for c in casadas}
        return encontradas.pop() if len(encontradas) == 1 else pd.NA

    vazias = df["marca"].isna()
    df.loc[vazias, "marca"] = df.loc[vazias, "nome"].map(achar)
    print("Marcas preenchidas pelo nome:", (vazias & df["marca"].notna()).sum(), "de", vazias.sum(), "vazias")
    return df


def ean_valido(ean):
    if not isinstance(ean, str) or len(ean) != 13 or not ean.isdigit():
        return False
    soma = 0
    for posicao, digito in enumerate(ean[:12]):
        peso = 1 if posicao % 2 == 0 else 3
        soma += int(digito) * peso
    verificador = (10 - soma % 10) % 10
    return verificador == int(ean[12])


def validar_ean(df):
    df = df.copy()
    df["ean_valido"] = df["ean"].apply(ean_valido)
    return df


def verificar_precos(df):
    df = df.copy()
    # Nem toda farmácia informa o preço no pix (a São João não informa).
    # Quando ele está vazio, só compara o preço do cartão com o preço sem desconto.
    pix_coerente = df["preco_pix"].isna() | (df["preco_pix"] <= df["preco_cartao"])
    df["preco_coerente"] = pix_coerente & (df["preco_cartao"] <= df["preco_sem_desconto"])
    df["desconto_alto"] = df["desconto_percentual"] > LIMITE_DESCONTO_ALTO
    return df


def remover_duplicados(df):
    antes = len(df)
    df = df.drop_duplicates(subset=["sku"], keep="first").copy()
    print("Duplicados por SKU removidos:", antes - len(df))

    ean_repetido = df["ean"].notna() & df.duplicated(subset=["ean"], keep=False)
    df["ean_repetido"] = ean_repetido
    return df


def salvar(df, farmacia, data_arquivo):
    caminho_csv = f"data/processed/{farmacia}_{data_arquivo}.csv"
    caminho_parquet = f"data/processed/{farmacia}_{data_arquivo}.parquet"
    caminho_xlsx = f"data/processed/{farmacia}_{data_arquivo}.xlsx"

    df.to_csv(caminho_csv, index=False, encoding="utf-8-sig")
    df.to_parquet(caminho_parquet, index=False)

    planilha = df.copy()
    planilha["categorias"] = planilha["categorias"].apply(
        lambda lista: " > ".join(lista) if isinstance(lista, list) else lista
    )
    planilha.to_excel(caminho_xlsx, index=False)

    print("Salvo:", caminho_csv)
    print("Salvo:", caminho_parquet)
    print("Salvo:", caminho_xlsx)


def tratar(farmacia, data_arquivo):
    caminho = f"data/raw/{farmacia}_{data_arquivo}.jsonl"

    df = ler_raw(caminho)
    print("Produtos no arquivo bruto:", len(df))

    df = converter_tipos(df)
    df = calcular_descontos(df)
    df = limpar_textos(df)
    if farmacia in FARMACIAS_MARCA_PELO_NOME:
        df = marca_pelo_nome(df)
    df = validar_ean(df)
    df = verificar_precos(df)
    df = remover_duplicados(df)

    print()
    print("Produtos na base tratada:", len(df))
    print("EANs inválidos ou vazios:", (~df["ean_valido"]).sum())
    print("Marcas vazias:", df["marca"].isna().sum())
    print("Preços incoerentes:", (~df["preco_coerente"]).sum())
    print(f"Descontos acima de {LIMITE_DESCONTO_ALTO}%:", df["desconto_alto"].sum())
    print("Produtos com EAN repetido em outro SKU:", df["ean_repetido"].sum())
    print()

    salvar(df, farmacia, data_arquivo)


if __name__ == "__main__":
    # Uso: python src/core/tratamento.py farmaponte
    #      python src/core/tratamento.py sao_joao
    # O nome da farmácia é o começo do arquivo em data/raw/ (ex.: sao_joao_2026-10-11.jsonl)
    farmacia = sys.argv[1] if len(sys.argv) > 1 else "farmaponte"
    data_arquivo = datetime.now().strftime("%Y-%m-%d")
    tratar(farmacia, data_arquivo)
