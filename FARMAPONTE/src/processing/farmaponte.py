import pandas as pd
from datetime import datetime

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", None)
pd.set_option("display.max_colwidth", 60)

CAMPOS_PRECO = ["preco_sem_desconto", "preco_cartao", "preco_pix"]
LIMITE_DESCONTO_ALTO = 80


def ler_raw(caminho):
    return pd.read_json(caminho, lines=True, dtype={"sku": str, "ean": str})


def converter_tipos(df):
    df = df.copy()
    for campo in CAMPOS_PRECO:
        df[campo] = pd.to_numeric(df[campo], errors="coerce")
    df["data_coleta"] = pd.to_datetime(df["data_coleta"])
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
    df["preco_coerente"] = (
        (df["preco_pix"] <= df["preco_cartao"])
        & (df["preco_cartao"] <= df["preco_sem_desconto"])
    )
    df["desconto_alto"] = df["desconto_percentual"] > LIMITE_DESCONTO_ALTO
    return df


def remover_duplicados(df):
    antes = len(df)
    df = df.drop_duplicates(subset=["sku"], keep="first").copy()
    print("Duplicados por SKU removidos:", antes - len(df))

    ean_repetido = df["ean"].notna() & df.duplicated(subset=["ean"], keep=False)
    df["ean_repetido"] = ean_repetido
    return df


def salvar(df, data_arquivo):
    caminho_csv = f"data/processed/farmaponte_{data_arquivo}.csv"
    caminho_parquet = f"data/processed/farmaponte_{data_arquivo}.parquet"
    caminho_xlsx = f"data/processed/farmaponte_{data_arquivo}.xlsx"

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


def tratar(data_arquivo):
    caminho = f"data/raw/farmaponte_{data_arquivo}.jsonl"

    df = ler_raw(caminho)
    print("Produtos no arquivo bruto:", len(df))

    df = converter_tipos(df)
    df = calcular_descontos(df)
    df = limpar_textos(df)
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

    salvar(df, data_arquivo)


if __name__ == "__main__":
    data_arquivo = datetime.now().strftime("%Y-%m-%d")
    tratar(data_arquivo)