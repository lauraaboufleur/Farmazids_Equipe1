"""Regras para ler, do nome do produto, a quantidade da embalagem, a unidade,
a concentração e a forma, e assim comparar preços por unidade.

As regex ficam só aqui. Delas saem:
- o SQL das views vw_precos_unitarios e vw_precos_unitarios_farmazids (gerar_sql);
- a função extrair, que repete em Python os mesmos passos do SQL, na mesma ordem,
  para testar as regras com os nomes coletados.

Para mudar uma regra: altere aqui, rode `python infra/athena/regras_extracao.py`
(regera os .sql desta pasta e mostra a cobertura) e rode o .sql novo no Athena.
"""
import re
from decimal import Decimal

# ---------- Regex (Python re e Joni/Trino entendem igual) ----------
ACENTOS_DE, ACENTOS_PARA = "áàâãäéèêëíìîïóòôõöúùûüç", "aaaaaeeeeiiiiooooouuuuc"
# separador de milhar: "25.000ui" -> 25000ui; com espaço só antes de UI ("10 000ui"), para "1 120 ml" não virar 1120
MILHAR = r"(?<![\d.,])([1-9]\d{0,2})(?:\.|\s(?=\d{3}\s*ui))(\d{3})(?![\d.,])"
C_BARRA = r"\bc/(\d)"                                       # c/20 -> c/ 20
NUM = r"(\d+(?:\.\d+)?)"
UNIDADES = (r"comprimid\w*|conprimid\w*|comp\.?|cprs?\.?|cpd|cp\.?|capsulas?|caps?\.?|cps|drageas?|drg"
            r"|saches?|sachets?|envelopes?|env|pastilhas?|gomas?|ampolas?|amp|flaconetes?|supositorios?"
            r"|ovulos?|adesivos?|seringas?|doses|acionamentos|jatos|unidades?|unid\.?|und\.?|un\.?")
# "c/20 cpr", "com 30 comprimidos", "caixa com 60 comprimidos": tem prioridade
RE_COM = rf"(?:\bc/|\bcom)\s*{NUM}\s*({UNIDADES})(?![a-z])"
# qualquer "<número> <unidade contável>": vale a última do nome
RE_CONTA = rf"(?<![\d.]){NUM}\s*({UNIDADES})(?![a-z])"
# volume/massa como quantidade; "/" antes ou depois indica concentração (500mg/5ml)
RE_VOL = rf"(?<![\d./]){NUM}\s*(ml|l|gr?|kg)(?![a-z/])(?!\s*/)"
POR = r"(?:\d+(?:\.\d+)?)?\s*(?:ml|g|dose|gota)"  # "por": /ml, /5ml, /g, /dose
RE_CONC = rf"(?<![\d.]){NUM}\s*(mg|mcg|ug|ui|%|g)(?:\s*/\s*({POR}))?(?![a-z])(?!\s+(?:de|cada)\s)"
RE_CONC_PARTES = rf"^{NUM}\s*(mg|mcg|ug|ui|%|g)(?:\s*/\s*({POR}))?"

# unidade escrita -> unidade padrão (ordem importa)
UNIDADE_PADRAO = [
    (r"^(comprimid\w*|conprimid\w*|comp\.?|cprs?\.?|cpd|cp\.?)$", "comprimido"),
    (r"^(capsulas?|caps?\.?|cps)$", "capsula"),
    (r"^(drageas?|drg)$", "dragea"),
    (r"^(saches?|sachets?|envelopes?|env)$", "sache"),
    (r"^(pastilhas?|gomas?)$", "pastilha"),
    (r"^(ampolas?|amp)$", "ampola"),
    (r"^flaconetes?$", "flaconete"),
    (r"^supositorios?$", "supositorio"),
    (r"^ovulos?$", "ovulo"),
    (r"^adesivos?$", "adesivo"),
    (r"^seringas?$", "seringa"),
    (r"^(doses|acionamentos|jatos)$", "dose"),
    (r"^(unidades?|unid\.?|und\.?|un\.?)$", "unidade"),
]
# forma (ordem importa: a primeira que aparecer no nome vence)
FORMAS = [
    (r"comprimid|conprimid|\bcomp\b|\bcpr|\bcp\b|\bcpd\b", "comprimido"),
    (r"capsula|\bcaps?\b|\bcps\b", "capsula"),
    (r"dragea|\bdrg\b", "dragea"),
    (r"sache|envelope", "sache"),
    (r"pastilha|\bgomas?\b", "pastilha"),
    (r"gotas|\bgts\b", "gotas"),
    (r"xarope", "xarope"),
    (r"injetavel|ampola|\bamp\b|seringa|caneta", "injetavel"),
    (r"suspensao", "suspensao"),
    (r"solucao|\bsol\b", "solucao"),
    (r"spray|aerossol|inalat", "spray"),
    (r"colirio", "colirio"),
    (r"pomada", "pomada"),
    (r"creme", "creme"),
    (r"\bgel\b", "gel"),
    (r"locao", "locao"),
    (r"supositorio", "supositorio"),
    (r"ovulo", "ovulo"),
    (r"adesivo", "adesivo"),
    (r"\bpo\b", "po"),
    (r"shampoo", "shampoo"),
]
SEMISSOLIDOS = ["pomada", "creme", "gel", "locao", "po", "shampoo"]
SOLIDOS_ORAIS = ["comprimido", "capsula", "dragea"]  # só estas ganham "efervescente"/"liberacao prolongada"
EFERVESCENTE = r"efervescente"
PROLONGADA = (r"(libera\w*|acao)\s+(prolongada|controlada|modificada|estendida|lenta|retardada)"
              r"|\b(xr|xl|cr|sr|la|lp|retard)\b")
SEM_ESTOQUE = 9999876


# ---------- Imitação em Python, passo a passo como no SQL ----------
def _fmt(x):
    s = f"{Decimal(x).quantize(Decimal('0.0001'))}"
    return re.sub(r"\.?0+$", "", s)


def extrair(nome):
    t = nome.lower()
    t = t.translate(str.maketrans(ACENTOS_DE, ACENTOS_PARA))
    t = re.sub(MILHAR, r"\1\2", t)
    t = t.replace(",", ".")
    t = re.sub(C_BARRA, r"c/ \1", t)
    t = " " + t + " "

    m = re.search(RE_COM, t)
    todos = [(x.group(1), x.group(2)) for x in re.finditer(RE_CONTA, t)]
    n_cont, u_cont = (m.group(1), m.group(2)) if m else (todos[-1] if todos else (None, None))
    unid_cont = next((p for r, p in UNIDADE_PADRAO if u_cont and re.search(r, u_cont)), None)
    qtd_cont = float(n_cont) if n_cont else None

    forma_base = next((f for r, f in FORMAS if re.search(r, t)), None)
    forma = forma_base
    if forma_base in SOLIDOS_ORAIS:
        if re.search(EFERVESCENTE, t):
            forma = forma_base + " efervescente"
        elif re.search(PROLONGADA, t):
            forma = forma_base + " liberacao prolongada"

    vols = [x.group(0) for x in re.finditer(RE_VOL, t)]
    vols = [v for v in vols if not re.search(r"\d\s*gr?$", v) or forma_base in SEMISSOLIDOS or forma_base is None]
    ultimo = vols[-1] if vols else None
    vn = float(re.search(NUM, ultimo).group(1)) if ultimo else None
    vu = re.search(r"(ml|l|gr?|kg)$", ultimo).group(1) if ultimo else None

    quantidade = qtd_cont if qtd_cont is not None else (vn * 1000 if vu in ("l", "kg") else vn)
    unidade = unid_cont if qtd_cont is not None else {"l": "ml", "kg": "g", "gr": "g"}.get(vu, vu)
    if quantidade is not None and quantidade <= 0:
        quantidade = unidade = None

    partes = []
    for c in re.finditer(RE_CONC, t):
        p = re.search(RE_CONC_PARTES, c.group(0))
        n, u, por = p.group(1), p.group(2), p.group(3)
        if u == "g" and por is None and (unid_cont is None or unid_cont in ("dose", "unidade")):
            continue
        valor = float(n) * 1000 if (u == "g" and por is None) else float(n)
        un = "mg" if (u == "g" and por is None) else "mcg" if u == "ug" else u
        partes.append(_fmt(valor) + un + ("/" + por.replace(" ", "") if por else ""))
    concentracao = "+".join(partes) or None
    return {"quantidade": quantidade, "unidade": unidade, "concentracao": concentracao, "forma": forma}


# ---------- Geração do SQL ----------
def _q(texto):
    """Literal de texto no SQL (Trino): só aspas simples precisam de escape."""
    return "'" + texto.replace("'", "''") + "'"


def _lista(valores):
    return ", ".join(_q(v) for v in valores)


def blocos_extracao():
    """Devolve (sql_normaliza, sql_passo1, sql_passo2, sql_colunas_finais) usados pelas duas views."""
    normaliza = (
        f"' ' || regexp_replace(replace(regexp_replace(translate(lower(nome), {_q(ACENTOS_DE)}, {_q(ACENTOS_PARA)}),\n"
        f"        {_q(MILHAR)}, '$1$2'), ',', '.'),\n"
        f"        {_q(C_BARRA)}, 'c/ $1') || ' '"
    )
    unidade_padrao = "CASE\n" + "\n".join(
        f"          WHEN regexp_like(u_cont, {_q(r)}) THEN {_q(p)}" for r, p in UNIDADE_PADRAO) + "\n        END"
    forma_base = "CASE\n" + "\n".join(
        f"          WHEN regexp_like(t, {_q(r)}) THEN {_q(f)}" for r, f in FORMAS) + "\n        END"
    passo1 = f"""    -- 1. quantidade contável ("c/20 cpr" e "com 30 comprimidos" primeiro; senão, a última do nome)
    COALESCE(regexp_extract(t, {_q(RE_COM)}, 1),
             element_at(regexp_extract_all(t, {_q(RE_CONTA)}, 1), -1)) AS n_cont,
    CASE WHEN regexp_like(t, {_q(RE_COM)}) THEN regexp_extract(t, {_q(RE_COM)}, 2)
         ELSE element_at(regexp_extract_all(t, {_q(RE_CONTA)}, 2), -1) END AS u_cont,
    -- forma (a primeira que aparecer na lista)
    {forma_base} AS forma_base"""
    passo2 = f"""    CAST(n_cont AS double) AS qtd_cont,
    {unidade_padrao} AS unid_cont,
    CASE
      WHEN forma_base IN ({_lista(SOLIDOS_ORAIS)}) AND regexp_like(t, {_q(EFERVESCENTE)}) THEN forma_base || ' efervescente'
      WHEN forma_base IN ({_lista(SOLIDOS_ORAIS)}) AND regexp_like(t, {_q(PROLONGADA)}) THEN forma_base || ' liberacao prolongada'
      ELSE forma_base
    END AS forma,
    -- volume/massa (só usado quando não há quantidade contável); "g" só em forma semissólida ou sem forma
    element_at(filter(regexp_extract_all(t, {_q(RE_VOL)}),
                      v -> NOT regexp_like(v, {_q(r'\d\s*gr?$')}) OR forma_base IN ({_lista(SEMISSOLIDOS)}) OR forma_base IS NULL),
               -1) AS vol"""
    fmt = "regexp_replace(CAST(CAST({x} AS decimal(18, 4)) AS varchar), '\\.?0+$', '')"
    valor = "CASE WHEN p.u = 'g' AND p.por IS NULL THEN CAST(p.n AS double) * 1000 ELSE CAST(p.n AS double) END"
    passo3 = f"""    CASE WHEN qtd_cont IS NOT NULL THEN qtd_cont
         WHEN regexp_extract(vol, '(ml|l|gr?|kg)$', 1) IN ('l', 'kg') THEN CAST(regexp_extract(vol, {_q(NUM)}, 1) AS double) * 1000
         ELSE CAST(regexp_extract(vol, {_q(NUM)}, 1) AS double) END AS qtd,
    CASE WHEN qtd_cont IS NOT NULL THEN unid_cont
         WHEN regexp_extract(vol, '(ml|l|gr?|kg)$', 1) = 'l' THEN 'ml'
         WHEN regexp_extract(vol, '(ml|l|gr?|kg)$', 1) IN ('kg', 'gr') THEN 'g'
         ELSE regexp_extract(vol, '(ml|l|gr?|kg)$', 1) END AS unid,
    -- concentração: todas as do nome, juntas com "+"; "1g" vira "1000mg"; "g" solto só conta em produto contável
    NULLIF(array_join(transform(
      filter(
        transform(regexp_extract_all(t, {_q(RE_CONC)}),
                  c -> CAST(ROW(regexp_extract(c, {_q(RE_CONC_PARTES)}, 1),
                                regexp_extract(c, {_q(RE_CONC_PARTES)}, 2),
                                regexp_extract(c, {_q(RE_CONC_PARTES)}, 3))
                            AS ROW(n varchar, u varchar, por varchar))),
        p -> NOT (p.u = 'g' AND p.por IS NULL AND (unid_cont IS NULL OR unid_cont IN ('dose', 'unidade')))),
      p -> {fmt.format(x=valor)}
           || CASE WHEN p.u = 'g' AND p.por IS NULL THEN 'mg' WHEN p.u = 'ug' THEN 'mcg' ELSE p.u END
           || COALESCE('/' || replace(p.por, ' ', ''), '')), '+'), '') AS concentracao"""
    return normaliza, passo1, passo2, passo3


def preco_por_unidade(coluna):
    return (f"CASE WHEN qtd > 0 AND TRY_CAST({coluna} AS double) > 0 AND TRY_CAST({coluna} AS double) <> {SEM_ESTOQUE}\n"
            f"       THEN round(TRY_CAST({coluna} AS double) / qtd, 4) END")


CABECALHO = """-- {titulo}
-- Gerado por infra/athena/regras_extracao.py: altere as regras lá e gere de novo, não edite à mão.
-- Não altera a view de origem ({origem}): só lê dela e acrescenta as colunas
--   quantidade, unidade, concentracao, forma e o preço por unidade.
-- Quando a quantidade não aparece no nome, ela fica nula (o preço por unidade também): nunca é chutada.
"""


def consulta(fonte, colunas_origem, precos):
    """WITH ... SELECT com as colunas da fonte mais as colunas novas."""
    normaliza, passo1, passo2, passo3 = blocos_extracao()
    cols = ", ".join(colunas_origem)
    precos_sql = "".join(f",\n  {preco_por_unidade(p)} AS {p}_por_unidade" for p in precos)
    return f"""WITH base AS (
  SELECT {cols},
    {normaliza} AS t
  FROM {fonte}
),
passo1 AS (
  SELECT base.*,
{passo1}
  FROM base
),
passo2 AS (
  SELECT passo1.*,
{passo2}
  FROM passo1
),
passo3 AS (
  SELECT passo2.*,
{passo3}
  FROM passo2
)
SELECT {cols},
  CASE WHEN qtd > 0 THEN qtd END AS quantidade,
  CASE WHEN qtd > 0 THEN unid END AS unidade,
  concentracao,
  forma{precos_sql}
FROM passo3"""


def gerar_sql(nome_view, origem, colunas_origem, precos, titulo):
    return (CABECALHO.format(titulo=titulo, origem=origem)
            + f"\nCREATE OR REPLACE VIEW t1_eq1_farmazids.{nome_view} AS\n"
            + consulta(f"t1_eq1_farmazids.{origem}", colunas_origem, precos) + "\n")


# Nomes reais com o resultado que o extrair dá (conferido à mão). O teste no Athena
# passa estes nomes pelo mesmo SQL das views e compara.
CASOS = [
    "Dipirona 1g 10 Comprimidos",
    "Dipirona 1.000mg 20 comprimidos",
    "PONDERA 15 MG C/20 CPR",
    "Dipirona Gotas 500mg/ml 20ml",
    "AMPLICTIL 4% GTS C/20 ML",
    "Zoladex 3,6mg 1 seringa",
    "Demedrox 150mg/ml Suspensão Injetável 1 seringa + 1agulha",
    "Tandene Diclofenaco Sódico 50mg + Carisoprodol 125mg + Paracetamol 300mg + Cafeína 30mg 30 comprimid",
    "Soapex Sabonete Liquido 1 120 Ml",
    "Hemax Eritron 10 000UI Inj 1Fr/Amp Biosintetica",
    "Nistatina Creme Vaginal Neo Química 25.000UI/g, caixa com 1 bisnaga com 60g de creme",
    "Drenison N 0,125mg/g + 0,35mg/g Creme 30g",
    "Biconcor 5,0mg Merck 30 comprimidos",
    "Lescol XL 80mg Novartis 30 Comprimidos",
    "Zomig Od 2,5mg 2 Comprimidos Orodispersíveis",
    "Cebion 1g 10 Comprimidos Efervescentes",
    "Glifage XR 500mg 30 comprimidos de liberação prolongada",
    "Aeroflux 2mg/5ml + 100mg/5ml GSK 120ml Solução",
    "Glyquin XM 40mg/g + 100mg/g, caixa com 28 sachês com 1g de creme de uso dermatológico",
    "Suplemento Alimentar Sarcopore Sabor Tangerina 30 Sachês com 16,10g Cada",
    "BALSAMO BENGUE AERO 120GR",
    "Ventus Spray 50mcg 60 Acionamentos 9ml",
    "Triancil 20mg/mL Frasco/ampola 5mL Injetável Apsen 1 Frasco",
    "Denyl 20mg comprimidos",
]


def gerar_teste():
    def lit(v):
        if v is None:
            return "CAST(NULL AS varchar)"
        return _q(v)

    linhas = []
    for nome in CASOS:
        e = extrair(nome)
        qtd = "CAST(NULL AS double)" if e["quantidade"] is None else f"CAST({e['quantidade']!r} AS double)"
        linhas.append(f"  ({_q(nome)}, {qtd}, {lit(e['unidade'])}, {lit(e['concentracao'])}, {lit(e['forma'])})")
    fonte = ("(VALUES\n" + ",\n".join(linhas)
             + "\n) AS casos(nome, esperado_quantidade, esperado_unidade, esperado_concentracao, esperado_forma)")
    cols = ["nome", "esperado_quantidade", "esperado_unidade", "esperado_concentracao", "esperado_forma"]
    sql = consulta(fonte, cols, [])
    # a consulta termina em "FROM passo3": vira mais um passo, e o SELECT final compara
    sql = sql.replace("\nSELECT nome, esperado_quantidade", "\n, resultado AS (\nSELECT nome, esperado_quantidade", 1)
    sql += """
)
SELECT nome,
  quantidade, esperado_quantidade, unidade, esperado_unidade,
  concentracao, esperado_concentracao, forma, esperado_forma,
  quantidade IS NOT DISTINCT FROM esperado_quantidade
    AND unidade IS NOT DISTINCT FROM esperado_unidade
    AND concentracao IS NOT DISTINCT FROM esperado_concentracao
    AND forma IS NOT DISTINCT FROM esperado_forma AS confere
FROM resultado
ORDER BY confere, nome
"""
    return ("-- Teste do SQL das views com nomes conhecidos (não cria nada no Athena).\n"
            "-- Gerado por infra/athena/regras_extracao.py. Esperado: a coluna confere = true em todas as linhas.\n"
            "-- Se alguma vier false, o SQL do Athena não está fazendo o mesmo que o teste em Python.\n\n" + sql)


# ---------- Gera os .sql e mostra a cobertura ----------
COLUNAS_CONCORRENTES = ["farmacia", "sku", "ean", "nome", "marca", "categorias", "preco_sem_desconto", "preco_cartao",
                        "preco_pix", "em_estoque", "desconto_valor", "desconto_percentual", "url", "data_coleta", "dt"]


def cobertura(raiz):
    """% de nomes com quantidade identificada, por farmácia, com os arquivos de data/ que existirem."""
    import pandas as pd
    fontes = {
        "farmaponte": raiz / "data/processed/farmaponte_2026-10-11.parquet",
        "sao_joao": raiz / "data/processed/sao_joao_2026-10-11.parquet",
        "veracruz": raiz / "data/processed/veracruz_2026-10-10.parquet",
        "sao_paulo": raiz / "data/amostras/sao_paulo_nomes.csv",
        "farmazids": raiz / "data/amostras/farmazids_amostra.csv",
    }
    for farmacia, arquivo in fontes.items():
        if not arquivo.exists():
            print(f"{farmacia:11} (sem arquivo {arquivo.relative_to(raiz)})")
            continue
        tabela = pd.read_parquet(arquivo) if arquivo.suffix == ".parquet" else pd.read_csv(arquivo, dtype=str)
        lidos = pd.DataFrame([extrair(n) for n in tabela["nome"].dropna().drop_duplicates()])
        remedios = lidos[lidos.concentracao.notna()]  # tem mg, mcg, UI ou % no nome
        print(f"{farmacia:11} {len(lidos):6} produtos: {100 * lidos.quantidade.notna().mean():5.1f}% com quantidade"
              f" | {len(remedios):6} medicamentos: {100 * remedios.quantidade.notna().mean():5.1f}% com quantidade")


if __name__ == "__main__":
    from pathlib import Path
    pasta = Path(__file__).parent
    (pasta / "vw_precos_unitarios.sql").write_text(gerar_sql(
        "vw_precos_unitarios", "vw_precos_concorrentes", COLUNAS_CONCORRENTES, ["preco_pix", "preco_cartao"],
        "Preço por unidade (comprimido, cápsula, ml, g...) dos concorrentes."), encoding="utf-8")
    (pasta / "vw_precos_unitarios_farmazids.sql").write_text(gerar_sql(
        "vw_precos_unitarios_farmazids", "vw_medicamentos", ["ean", "nome", "preco_farmazids"], ["preco_farmazids"],
        "Preço por unidade da tabela de preços da Farmazids (sem data de coleta nem estoque)."), encoding="utf-8")
    (pasta / "teste_casos_conhecidos.sql").write_text(gerar_teste(), encoding="utf-8")
    print("SQL gerado em", pasta)
    cobertura(pasta.parent.parent)
