import asyncio
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import aiohttp
from yarl import URL

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}

URL_BASE = "https://www.drogariasaopaulo.com.br"
URL_API = f"{URL_BASE}/api/catalog_system/pub/products/search"

FARMACIA = "sao_paulo"

# VERACRUZ/ (este arquivo fica em VERACRUZ/src/adapters/)
PASTA_BASE = Path(__file__).resolve().parents[2]
PASTA_RAW = PASTA_BASE / "data" / "raw"

CATEGORIA_ID = 800  # Medicamentos (id da árvore de categorias da loja)
POR_PAGINA = 50  # a API não aceita mais de 50 produtos por chamada
LIMITE_FATIA = 2500  # a API recusa _from > 2500; cada fatia de preço precisa caber nisso
PRECO_MAXIMO = 1_000_000  # limite superior da faixa de preço usada para fatiar
# Faixas que não dá mais para dividir por preço (ex.: os produtos fora de estoque, que a
# busca indexa com preço R$ 0,00) são lidas várias vezes, cada vez em uma ordenação
# diferente; cada ordenação traz até LIMITE_FATIA produtos e a união cobre a faixa inteira
# (NameASC + NameDESC sozinhas já cobrem até 2 x LIMITE_FATIA).
ORDENS_DESEMPATE = [
    "OrderByNameASC",
    "OrderByNameDESC",
    "OrderByReleaseDateDESC",
    "OrderByTopSaleDESC",
    "OrderByBestDiscountDESC",
]
# Especificação usada como subcategoria (a árvore da loja não tem filhos em Medicamentos)
CAMPO_SUBCATEGORIA = "Classe do Medicamento"
COBERTURA_MINIMA = 0.95  # abaixo disso a coleta termina com erro (arquivo é salvo mesmo assim)

# Total de requisições simultâneas (cada resposta pesa ~1 MB: não aumente muito)
MAX_REQUISICOES = 6
TENTATIVAS = 4
TIMEOUT = 60
STATUS_OK = {200, 206}  # a API de catálogo da VTEX responde 206 quando devolve parte da lista
STATUS_RETRY = {429, 500, 502, 503, 504}

# Mesmas colunas, na mesma ordem, do veracruz.py (o tratamento compartilhado depende disso)
COLUNAS = [
    "sku",
    "ean",
    "nome",
    "marca",
    "categorias",
    "preco_sem_desconto",
    "preco_cartao",
    "preco_pix",
    "em_estoque",
    "url",
    "farmacia",
    "data_coleta",
    "desconto",  # % calculado a partir dos preços; o tratamento descarta e recalcula
]


# --------------------------------------------------------------------------
# Requisição
# --------------------------------------------------------------------------


async def requisitar(sessao, semaforo, params):
    # GET com retry. Retorna (status, json, total, erro).
    # "total" vem do header resources (ex.: "0-49/11629" -> 11629).
    query = "&".join(f"{quote(str(k), safe='')}={quote(str(v), safe='')}" for k, v in params)
    url = URL(f"{URL_API}?{query}", encoded=True)  # encoded=True: não deixa o aiohttp recodificar

    status, erro = None, None
    for tentativa in range(TENTATIVAS):
        try:
            async with semaforo, sessao.get(
                url, timeout=aiohttp.ClientTimeout(total=TIMEOUT)
            ) as resposta:
                status = resposta.status
                if status in STATUS_OK:
                    dados = await resposta.json(content_type=None)
                    achado = re.search(r"/(\d+)\s*$", resposta.headers.get("resources", ""))
                    return status, dados, int(achado.group(1)) if achado else None, None
                if status not in STATUS_RETRY:
                    return status, None, None, None
        except Exception as e:
            erro = e
        await asyncio.sleep(2**tentativa)
    return status, None, None, erro


def filtros(preco_min=None, preco_max=None, ordem=None):
    # Categoria Medicamentos (+ faixa de preço e ordenação, quando informadas)
    params = [("fq", f"C:{CATEGORIA_ID}")]
    if preco_min is not None:
        params.append(("fq", f"P:[{preco_min:.2f} TO {preco_max:.2f}]"))
    if ordem:
        params.append(("O", ordem))
    return params


async def contar(sessao, semaforo, preco_min=None, preco_max=None):
    # Quantos produtos existem na faixa: pede só 1 produto e lê o total no header resources
    status, dados, total, erro = await requisitar(
        sessao, semaforo, filtros(preco_min, preco_max) + [("_from", 0), ("_to", 0)]
    )
    if status not in STATUS_OK:
        raise RuntimeError(f"falha ao contar produtos (status={status}, erro={erro!r})")
    if not dados:
        return 0
    if total is None:
        raise RuntimeError("a resposta não trouxe o header 'resources' com o total")
    return total


async def fatiar(sessao, semaforo, preco_min, preco_max, total=None):
    # Divide a faixa de preço ao meio, recursivamente, até cada fatia ter no máximo
    # LIMITE_FATIA produtos. Retorna uma lista de (preco_min, preco_max, total).
    # Obs.: o filtro P: da API usa o preço de venda indexado, que é 0 para produto
    # indisponível; não existe filtro pelo ListPrice (preço sem desconto).
    if total is None:
        total = await contar(sessao, semaforo, preco_min, preco_max)
    if total == 0:
        return []
    if total <= LIMITE_FATIA:
        return [(preco_min, preco_max, total)]
    if preco_max - preco_min <= 0.01:
        # Não dá para dividir mais por preço: coletar_fatia lê essa faixa em várias ordenações
        print(
            f"Faixa R$ {preco_min:.2f} tem {total} produtos (> {LIMITE_FATIA}); "
            f"será lida em {len(ORDENS_DESEMPATE)} ordenações."
        )
        if total > LIMITE_FATIA * len(ORDENS_DESEMPATE):
            print(f"AVISO: {total} produtos é mais do que as ordenações conseguem cobrir.")
        return [(preco_min, preco_max, total)]

    meio = round((preco_min + preco_max) / 2, 2)
    esquerda = (preco_min, meio)
    direita = (round(meio + 0.01, 2), preco_max)
    t_esq, t_dir = await asyncio.gather(
        contar(sessao, semaforo, *esquerda), contar(sessao, semaforo, *direita)
    )
    if t_esq == total and t_dir == total:
        raise RuntimeError(
            "o filtro de preço (fq=P:[a TO b]) não reduziu o total: a API parece estar ignorando-o"
        )
    esq, dir_ = await asyncio.gather(
        fatiar(sessao, semaforo, *esquerda, t_esq),
        fatiar(sessao, semaforo, *direita, t_dir),
    )
    return esq + dir_


# --------------------------------------------------------------------------
# Extração dos campos
# --------------------------------------------------------------------------


def escolher_oferta(item):
    # Oferta do vendedor padrão (a Drogaria São Paulo); se não houver, a primeira
    vendedores = item.get("sellers") or []
    if not vendedores:
        return None
    vendedor = next((v for v in vendedores if v.get("sellerDefault")), vendedores[0])
    return vendedor.get("commertialOffer")


def preco_pix(oferta):
    # Valor do "Pix à vista" na lista de formas de pagamento
    for parcela in oferta.get("Installments") or []:
        if parcela.get("PaymentSystemName") == "Pix" and parcela.get("NumberOfInstallments") == 1:
            return parcela.get("Value")
    return None


def categorias_do_produto(produto):
    # Caminho mais fundo de "categories" (aqui é sempre só "/Medicamentos/") seguido das
    # subcategorias, que a loja guarda na especificação "Classe do Medicamento"
    # (ex.: ["Medicamentos", "Antidiabéticos", "Hipoglicemiantes"])
    caminhos = [c for c in (produto.get("categories") or []) if c and c.strip("/")]
    categorias = []
    if caminhos:
        mais_fundo = max(caminhos, key=lambda c: c.strip("/").count("/"))
        categorias = [parte for parte in mais_fundo.strip("/").split("/") if parte]
    for sub in produto.get(CAMPO_SUBCATEGORIA) or []:
        sub = (sub or "").strip()
        if sub and sub not in categorias:
            categorias.append(sub)
    return categorias or None


def extrair_linhas(produto, data_coleta):
    # Uma linha por SKU (item) do produto
    categorias = categorias_do_produto(produto)
    linhas = []
    for item in produto.get("items") or []:
        oferta = escolher_oferta(item) or {}

        # Price = preço de venda (igual ao do cartão à vista); ListPrice = preço "de" (riscado)
        preco = oferta.get("Price")
        preco = round(preco, 2) if preco and preco > 0 else None
        lista = oferta.get("ListPrice")
        if lista and lista > 0 and (preco is None or lista > preco):
            preco_sem_desconto = round(lista, 2)
        else:
            preco_sem_desconto = preco
        pix = preco_pix(oferta)
        pix = round(pix, 2) if pix and pix > 0 else None
        if pix is None:
            # Sem Pix na lista de pagamentos (acontece com produto fora de estoque)
            pix = preco

        desconto = None
        if preco is not None and preco_sem_desconto and preco_sem_desconto > preco:
            desconto = round((1 - preco / preco_sem_desconto) * 100)

        disponivel = oferta.get("IsAvailable")
        ean = (item.get("ean") or "").strip() or None

        linhas.append(
            {
                "sku": str(item.get("itemId")),
                "ean": ean,
                "nome": item.get("nameComplete") or produto.get("productName"),
                "marca": produto.get("brand"),
                "categorias": categorias,
                "preco_sem_desconto": preco_sem_desconto,
                "preco_cartao": preco,
                "preco_pix": pix,
                "em_estoque": None if disponivel is None else bool(disponivel),
                "url": produto.get("link"),
                "farmacia": FARMACIA,
                "data_coleta": data_coleta,
                "desconto": desconto,
            }
        )
    return linhas


# --------------------------------------------------------------------------
# Coleta
# --------------------------------------------------------------------------


async def coletar_pagina(
    sessao, semaforo, fatia, ordem, inicio, linhas, produtos_vistos, falhas
):
    preco_min, preco_max, _ = fatia
    params = filtros(preco_min, preco_max, ordem) + [
        ("_from", inicio),
        ("_to", inicio + POR_PAGINA - 1),
    ]
    status, dados, _, erro = await requisitar(sessao, semaforo, params)

    if status not in STATUS_OK or dados is None:
        # status HTTP (ex.: 429) ou o tipo do erro (ex.: TimeoutError)
        falhas[status if status is not None else type(erro).__name__] += 1
        return

    data_coleta = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for produto in dados:
        produtos_vistos.add(produto.get("productId"))
        for linha in extrair_linhas(produto, data_coleta):
            linhas.setdefault(linha["sku"], linha)  # SKU repetido em outra fatia: fica o primeiro


async def coletar_fatia(sessao, semaforo, fatia, linhas, produtos_vistos, falhas):
    # Fatia que cabe no limite: uma leitura na ordem padrão. Fatia maior (só acontece
    # quando a faixa de preço não pode mais ser dividida): uma leitura por ordenação.
    total = fatia[2]
    ordens = [None] if total <= LIMITE_FATIA else ORDENS_DESEMPATE
    paginas = range(0, min(total, LIMITE_FATIA), POR_PAGINA)
    await asyncio.gather(
        *(
            coletar_pagina(
                sessao, semaforo, fatia, ordem, inicio, linhas, produtos_vistos, falhas
            )
            for ordem in ordens
            for inicio in paginas
        )
    )


def salvar_jsonl(linhas):
    # Um produto por linha (JSON Lines): data/raw/sao_paulo_AAAA-MM-DD.jsonl
    PASTA_RAW.mkdir(parents=True, exist_ok=True)
    arquivo = PASTA_RAW / f"{FARMACIA}_{datetime.now():%Y-%m-%d}.jsonl"
    with open(arquivo, "w", encoding="utf-8") as f:
        for linha in linhas:
            registro = {coluna: linha.get(coluna) for coluna in COLUNAS}
            f.write(json.dumps(registro, ensure_ascii=False) + "\n")
    return arquivo


async def main():
    semaforo = asyncio.Semaphore(MAX_REQUISICOES)
    conector = aiohttp.TCPConnector(limit=0, ttl_dns_cache=300)
    linhas, produtos_vistos, falhas = {}, set(), Counter()

    async with aiohttp.ClientSession(headers=headers, connector=conector) as sessao:
        try:
            esperado = await contar(sessao, semaforo)
            print(f"Produtos na categoria {CATEGORIA_ID} (header resources): {esperado}")

            fatias = await fatiar(sessao, semaforo, 0.0, float(PRECO_MAXIMO))
        except RuntimeError as e:
            raise SystemExit(f"ERRO: {e}")

        print(f"{len(fatias)} fatias de preço (até {LIMITE_FATIA} produtos cada):")
        for preco_min, preco_max, total in fatias:
            print(f"  R$ {preco_min:.2f} a R$ {preco_max:.2f}: {total} produtos")

        await asyncio.gather(
            *(
                coletar_fatia(sessao, semaforo, fatia, linhas, produtos_vistos, falhas)
                for fatia in fatias
            )
        )

    ordenadas = sorted(linhas.values(), key=lambda linha: (len(linha["sku"]), linha["sku"]))
    arquivo = salvar_jsonl(ordenadas) if ordenadas else None
    cobertura = len(produtos_vistos) / esperado if esperado else 0

    print("--- Diagnóstico da coleta ---")
    print("Arquivo salvo:", arquivo or "nenhum (0 produtos coletados)")
    print("Produtos esperados (resources):", esperado)
    print("Produtos coletados:", len(produtos_vistos), f"({cobertura:.1%})")
    print("SKUs (linhas) coletados:", len(ordenadas))
    print("Páginas que falharam (status/erro -> qtd):", dict(falhas))

    if not ordenadas:
        raise SystemExit("ERRO: nenhum produto coletado. Nada foi salvo.")
    if cobertura < COBERTURA_MINIMA:
        raise SystemExit(
            f"ERRO: só {cobertura:.1%} dos produtos esperados foram coletados "
            f"(mínimo {COBERTURA_MINIMA:.0%}). O arquivo foi salvo, mas a coleta falhou."
        )


if __name__ == "__main__":
    asyncio.run(main())
