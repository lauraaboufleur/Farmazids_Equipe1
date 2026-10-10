import argparse
import html
import re
import unicodedata
from pathlib import Path
 
import pandas as pd
 
FARMACIA = "Drogaria VeraCruz"
 
# raiz do repositório (este arquivo fica em src/core/)
PASTA_BASE = Path(__file__).resolve().parents[2]
PASTA_RAW = PASTA_BASE / "data" / "raw"
PASTA_PROCESSED = PASTA_BASE / "data" / "processed"
# Nome que o veracruz.py de coleta usa hoje (grava na pasta onde foi executado)
ARQUIVO_COLETA_ANTIGO = PASTA_BASE / "produtos_drogaria_veracruz.parquet"
 
LIMITE_DESCONTO_ALTO = 80  # em %
 
COLUNAS_FINAIS = [
    "ean", "sku", "nome", "marca", "farmacia",
    "preco_sem_desconto", "preco_com_desconto",
    "desconto_valor", "desconto_percentual",
    "data_coleta",
    "ean_valido", "preco_coerente", "desconto_alto", "ean_repetido", "preco_ausente",
]
 
 
# --------------------------------------------------------------------------
# Funções auxiliares
# --------------------------------------------------------------------------
 
 
def limpar_textos(serie):
    """Limpa nome/marca: entidades HTML, espaços estranhos, espaços duplos e vazios."""
 
    def limpar(valor):
        if pd.isna(valor):
            return pd.NA
        texto = html.unescape(str(valor))
        texto = unicodedata.normalize("NFC", texto)
        texto = texto.replace("\xa0", " ")
        texto = re.sub(r"\s+", " ", texto).strip()
        return texto if texto else pd.NA
 
    return serie.map(limpar).astype("string")
 
 
def ean_valido(codigo):
    """True se o código tem 8, 12, 13 ou 14 dígitos e o dígito verificador (GS1) confere."""
    if pd.isna(codigo):
        return False
    codigo = str(codigo).strip()
    if not codigo.isdigit() or len(codigo) not in (8, 12, 13, 14):
        return False
 
    digitos = [int(c) for c in codigo]
    verificador, corpo = digitos[-1], digitos[:-1]
    # Da direita para a esquerda, os pesos alternam 3, 1, 3, 1...
    soma = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(corpo)))
    return (10 - soma % 10) % 10 == verificador
 
 
def encontrar_arquivo_bruto():
    """Pega o parquet mais recente em data/raw/ (ou o nome antigo da coleta)."""
    if PASTA_RAW.exists():
        candidatos = sorted(PASTA_RAW.glob("*.parquet"), key=lambda p: p.stat().st_mtime)
        if candidatos:
            return candidatos[-1]
    if ARQUIVO_COLETA_ANTIGO.exists():
        return ARQUIVO_COLETA_ANTIGO
    raise FileNotFoundError(
        f"Nenhum parquet bruto encontrado em {PASTA_RAW} nem em {ARQUIVO_COLETA_ANTIGO}. "
        "Rode a coleta primeiro ou use --entrada."
    )
 
 
# --------------------------------------------------------------------------
# Tratamento
# --------------------------------------------------------------------------
 
 
def tratar(bruto):
    df = bruto.copy()
 
    # 2. Renomear para o padrão comum
    df = df.rename(columns={
        "gtin13": "ean",
        "preco_original": "preco_sem_desconto",
        "preco_desconto": "preco_com_desconto",
    })
 
    # Guarda se a coleta informou desconto (usado só no resumo do passo 9)
    desconto_informado = pd.to_numeric(
        df.get("desconto", pd.Series(0, index=df.index)), errors="coerce"
    ).fillna(0) > 0
 
    # 3. Farmácia
    df["farmacia"] = pd.Series(FARMACIA, index=df.index, dtype="string")
 
    # 4. Tipos: data_coleta vira datetime; sku e ean ficam texto (não perde zero à esquerda)
    df["data_coleta"] = pd.to_datetime(df["data_coleta"], format="%d/%m/%Y %H:%M:%S", errors="coerce")
    df["sku"] = df["sku"].astype("string").str.strip()
    df["ean"] = df["ean"].astype("string").str.strip()
 
    for coluna in ("preco_sem_desconto", "preco_com_desconto"):
        df[coluna] = pd.to_numeric(df[coluna], errors="coerce").astype("float64")
 
    # 5. Sem desconto: o preço com desconto é o próprio preço normal
    #    (senão esses produtos somem de consultas do tipo "menor preço")
    sem_preco_final = df["preco_com_desconto"].isna() & df["preco_sem_desconto"].notna()
    df.loc[sem_preco_final, "preco_com_desconto"] = df.loc[sem_preco_final, "preco_sem_desconto"]
 
    # 6. Recalcular o desconto a partir dos preços
    tem_precos = df["preco_sem_desconto"].notna() & df["preco_com_desconto"].notna()
    df["desconto_valor"] = (df["preco_sem_desconto"] - df["preco_com_desconto"]).where(tem_precos).round(2)
    base_valida = tem_precos & (df["preco_sem_desconto"] > 0)
    df["desconto_percentual"] = (
        df["desconto_valor"] / df["preco_sem_desconto"] * 100
    ).where(base_valida).round(2)
 
    # 7. Limpar textos
    df["nome"] = limpar_textos(df["nome"])
    df["marca"] = limpar_textos(df["marca"])
 
    # 8. Validações
    df["ean_valido"] = df["ean"].map(ean_valido).astype("bool")
    df["preco_coerente"] = (
        tem_precos & (df["preco_com_desconto"] <= df["preco_sem_desconto"] + 0.005)
    ).astype("bool")
    df["desconto_alto"] = (df["desconto_percentual"] > LIMITE_DESCONTO_ALTO).fillna(False).astype("bool")
 
    # duplicados por sku (mantém a primeira ocorrência; sku vazio não conta como duplicado)
    repetido_sku = df["sku"].notna() & df["sku"].duplicated(keep="first")
    removidos = int(repetido_sku.sum())
    desconto_informado = desconto_informado[~repetido_sku]
    df = df[~repetido_sku].copy()
 
    # mesmo EAN em SKUs diferentes: marca todas as linhas envolvidas
    df["ean_repetido"] = (df["ean"].notna() & df["ean"].duplicated(keep=False)).astype("bool")
 
    # 9. Produto sem nenhum preço. (Depois do passo 5, se existe o preço normal o preço
    #    com desconto também existe; então "sem preço" = os dois vazios.)
    sem_preco = df["preco_sem_desconto"].isna()
    com_preco = df["preco_com_desconto"].isna()
    df["preco_ausente"] = (sem_preco & com_preco).astype("bool")
    # Produtos que só mostram o preço com desconto (ex.: alguns PBM): têm preço,
    # mas não dá para recalcular o desconto. Só entram na contagem do resumo.
    so_preco_com_desconto = int((sem_preco & ~com_preco).sum())
    ambos_precos = ~sem_preco & ~com_preco
 
    resumo = {
        "linhas_brutas": len(bruto),
        "duplicados_sku_removidos": removidos,
        "linhas_finais": len(df),
        "ean_invalido": int((~df["ean_valido"]).sum()),
        "preco_incoerente": int((ambos_precos & ~df["preco_coerente"]).sum()),
        "desconto_alto": int(df["desconto_alto"].sum()),
        "ean_repetido": int(df["ean_repetido"].sum()),
        "preco_ausente": int(df["preco_ausente"].sum()),
        "so_preco_com_desconto": so_preco_com_desconto,
        "com_desconto_informado_e_sem_preco": int((df["preco_ausente"] & desconto_informado).sum()),
    }
    return df[COLUNAS_FINAIS].reset_index(drop=True), resumo
 
 
# --------------------------------------------------------------------------
# Execução
# --------------------------------------------------------------------------
 
 
def main():
    parser = argparse.ArgumentParser(description="Tratamento dos dados da Drogaria VeraCruz")
    parser.add_argument("--entrada", help="parquet bruto (padrão: o mais recente em data/raw/)")
    args = parser.parse_args()
 
    entrada = Path(args.entrada) if args.entrada else encontrar_arquivo_bruto()
    print(f"1. Lendo {entrada}")
    bruto = pd.read_parquet(entrada)
 
    df, resumo = tratar(bruto)
 
    # 10. Salvar só em parquet, com a data da coleta no nome
    data = df["data_coleta"].dropna().max()
    sufixo = data.strftime("%Y-%m-%d") if pd.notna(data) else pd.Timestamp.today().strftime("%Y-%m-%d")
    PASTA_PROCESSED.mkdir(parents=True, exist_ok=True)
    saida = PASTA_PROCESSED / f"veracruz_{sufixo}.parquet"
    df.to_parquet(saida, index=False, engine="pyarrow")
 
    print("\n--- Resumo ---")
    for chave, valor in resumo.items():
        print(f"{chave:38s} {valor}")
    print(f"\n10. Salvo: {saida}")
 
    ausentes = df[df["preco_ausente"]]
    if len(ausentes):
        print(f"\nProdutos sem preço (confira no site se o preço aparece de outro jeito): {len(ausentes)}")
        print(ausentes[["sku", "nome"]].head(10).to_string(index=False))
 
 
if __name__ == "__main__":
    main()
