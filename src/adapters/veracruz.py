import asyncio
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import aiohttp
from bs4 import BeautifulSoup

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}

# Total de requisições simultâneas (páginas + produtos).
MAX_REQUISICOES = 40

# Tentativas por requisição (com espera crescente entre elas) para não
# perder produtos por falha momentânea de rede ou limite do servidor
TENTATIVAS = 4
STATUS_RETRY = {429, 500, 502, 503, 504}

URL_BASE = "https://www.drogariaveracruz.com.br"
FARMACIA = "VeraCruz"

# VERACRUZ/ (este arquivo fica em VERACRUZ/src/adapters/)
PASTA_BASE = Path(__file__).resolve().parents[2]
PASTA_RAW = PASTA_BASE / "data" / "raw"

COLUNAS = [
    "sku",
    "gtin13",
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
]


# Páginas de produto: tempo máximo por tentativa (são páginas pesadas)
TIMEOUT_PRODUTO = 20

# Diagnóstico da coleta (resumo impresso no final)
PASTA_DEBUG = PASTA_BASE / "debug_sem_preco"  # HTML de páginas que vieram sem preço
MAX_DEBUG = 10
falhas_produto = Counter()  # páginas de produto que não deram status 200
contadores = Counter()  # de onde veio o preço de cada produto


def limpar_preco(texto):
    # Converte R$ 50,02 em float: 50.02.
    if not texto:
        return None

    # Formato brasileiro: 1.234,56 ou 50,02
    achado = re.search(r"\d{1,3}(?:\.\d{3})*,\d+|\d+,\d+", texto)
    if achado:
        return float(achado.group().replace(".", "").replace(",", "."))

    # Número com ponto decimal (ex.: '9.27') ou inteiro
    achado = re.search(r"\d+(?:\.\d+)?", texto)
    if achado:
        return float(achado.group())

    return None


async def baixar(sessao, semaforo, url, timeout):
    # Faz o GET com retry. Retorna (status, texto, erro).
    status, erro = None, None
    for tentativa in range(TENTATIVAS):
        try:
            # O semáforo fica só em volta da requisição, então o timeout
            # só começa a contar depois que a vaga é liberada
            async with semaforo, sessao.get(
                url, timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resposta:
                status = resposta.status
                if status == 200:
                    return status, await resposta.text(), None
                if status not in STATUS_RETRY:
                    return status, None, None
        except Exception as e:
            erro = e
        await asyncio.sleep(2**tentativa)
    return status, None, erro


def extrair_dados_produto(texto):
    # Extrai tudo da página individual do produto.
    # Os preços vêm daqui (e não do card da listagem): no card dos produtos com
    # desconto de laboratório o bloco de preço muda, mas a página do produto
    # sempre tem os mesmos três preços (sem desconto, cartão e Pix).

    # SKU, GTIN-13 e Marca ficam no JSON-LD do <head>: regex direto no texto
    match_sku = re.search(r'"sku"\s*:\s*"(\d+)"', texto)
    match_gtin = re.search(r'"gtin13"\s*:\s*"(\d+)"', texto)
    match_marca = re.search(
        r'"brand"\s*:\s*\{[^}]*?"name"\s*:\s*"([^"]+)"', texto
    )
    # O "price" do JSON-LD é o preço Pix (usado só se o selo Pix não aparecer).
    # Não usar window.dataProduct.price: nos produtos parcelados ele traz o
    # valor de UMA parcela (ex.: 61.19 em vez de 122.37).
    match_pix_json = re.search(
        r'"offers"\s*:\s*\{[^}]*?"price"\s*:\s*"([\d.]+)"', texto
    )
    # Estoque: "availability":"http://schema.org/InStock" ou ".../OutOfStock"
    match_estoque = re.search(
        r'"offers"\s*:\s*\{[^}]*?"availability"\s*:\s*"[^"]*?/(\w+)"', texto
    )
    # Qualquer outro valor (ou ausência) fica None, em vez de chutar True/False
    em_estoque = {"InStock": True, "OutOfStock": False}.get(
        match_estoque.group(1) if match_estoque else None
    )
    # Reserva dos preços: a página também embute os valores em <script>, mesmo
    # quando o bloco de preços do HTML não aparece.
    #   dataProduct.sale_price = preço no cartão
    #   "total_price"          = preço sem desconto
    match_cartao_js = re.search(
        r'window\.dataProduct\s*=\s*\{[^}]*?"sale_price"\s*:\s*"([\d.]+)"', texto
    )
    match_total_js = re.search(r'"total_price"\s*:\s*([\d.]+)', texto)

    # Só o miolo da página (sem os menus gigantes) vai para o BeautifulSoup.
    # Se os marcadores não existirem, parseia a página inteira.
    inicio = texto.find('id="content-product"')
    fim = texto.find('id="description"')
    trecho = texto[inicio:fim] if 0 <= inicio < fim else texto
    soup = BeautifulSoup(trecho, "lxml")

    # Nome
    tag_nome = soup.select_one("h1.name")
    nome = tag_nome.get_text(strip=True) if tag_nome else None

    # Categorias: migalhas de pão (breadcrumb) sem o "Início"
    categorias = [
        li.get_text(strip=True) for li in soup.select("#breadcrumb li:not(.home)")
    ]
    categorias = " > ".join(c for c in categorias if c) or None

    # Preços. Buscar só dentro de div.prices evita pegar o preço Pix
    # (<p class="seal-pix ... sale-price">), que também tem a classe
    # "sale-price" e aparece antes no HTML. O bloco de desconto de laboratório
    # (div.pbm-section) não é lido de propósito.
    tag_preco_orig = soup.select_one("div.prices p.unit-price")
    tag_preco_cartao = soup.select_one("div.prices p.sale-price")
    tag_preco_pix = soup.select_one("p.seal-pix")

    preco_sem_desconto = limpar_preco(
        tag_preco_orig.get_text(strip=True) if tag_preco_orig else None
    )
    preco_cartao = limpar_preco(
        tag_preco_cartao.get_text(" ", strip=True) if tag_preco_cartao else None
    )
    preco_pix = limpar_preco(
        tag_preco_pix.get_text(" ", strip=True) if tag_preco_pix else None
    )
    if preco_pix is None and match_pix_json:
        preco_pix = float(match_pix_json.group(1))

    # Sem o bloco de preços no HTML: usa os valores dos scripts (0 não conta como preço)
    if preco_cartao is None and preco_sem_desconto is None:
        if match_cartao_js:
            preco_cartao = float(match_cartao_js.group(1)) or None
        if match_total_js:
            preco_sem_desconto = float(match_total_js.group(1)) or None

    # Sem desconto: só aparece um preço, que serve para os dois
    if preco_cartao is None:
        preco_cartao = preco_sem_desconto
    if preco_sem_desconto is None:
        preco_sem_desconto = preco_cartao

    return {
        "sku": match_sku.group(1) if match_sku else None,
        "gtin13": match_gtin.group(1) if match_gtin else None,
        "nome": nome,
        "marca": match_marca.group(1) if match_marca else None,
        "categorias": categorias,
        "preco_sem_desconto": preco_sem_desconto,
        "preco_cartao": preco_cartao,
        "preco_pix": preco_pix,
        "em_estoque": em_estoque,
    }


async def buscar_dados_da_pagina_produto(sessao, semaforo, url_produto):
    # Acessa a página individual do produto uma vez e extrai todos os dados.
    if not url_produto:
        return None

    status, texto, erro = await baixar(
        sessao, semaforo, urljoin(URL_BASE, url_produto), timeout=TIMEOUT_PRODUTO
    )
    if status != 200:
        # status HTTP (ex.: 429, 403) ou o tipo do erro (ex.: TimeoutError)
        motivo = status if status is not None else type(erro).__name__
        falhas_produto[motivo] += 1
        return None

    dados = extrair_dados_produto(texto)

    # Página veio (200) mas sem preço: guarda o HTML das primeiras para conferir
    if dados["preco_cartao"] is None and dados["preco_sem_desconto"] is None:
        contadores["pagina_sem_preco"] += 1
        if contadores["pagina_sem_preco"] <= MAX_DEBUG:
            Path(PASTA_DEBUG).mkdir(exist_ok=True)
            nome_arquivo = re.sub(r"[^a-z0-9]+", "-", url_produto.lower()).strip("-")[:80]
            (Path(PASTA_DEBUG) / f"{nome_arquivo}.html").write_text(
                texto, encoding="utf-8"
            )

    return dados


def extrair_precos_bloco(bloco):
    # Preços do card da listagem. Só são usados se a página do produto falhar
    # ou vier sem preço (os preços "oficiais" vêm da página do produto).
    # Buscar dentro de div.box-prices e ignorar o selo Pix evita pegar o preço Pix.
    box_precos = bloco.find("div", class_="box-prices") or bloco

    tag_orig = box_precos.find("p", class_="unit-price")
    tag_cartao = next(
        (
            p
            for p in box_precos.find_all("p", class_="sale-price")
            if "seal-pix" not in p.get("class", [])
        ),
        None,
    )
    # Produto PBM / desconto de laboratório: o card pode trazer só este bloco
    if not tag_cartao:
        tag_cartao = bloco.find("div", class_="pbm-prices")

    preco_sem_desconto = limpar_preco(
        tag_orig.get_text(strip=True) if tag_orig else None
    )
    preco_cartao = limpar_preco(
        tag_cartao.get_text(" ", strip=True) if tag_cartao else None
    )
    return preco_sem_desconto, preco_cartao


def extrair_dados_bloco(bloco):
    # Da listagem pegamos o SKU, a URL (para abrir a página do produto), o nome
    # e os preços do card (estes dois últimos como reserva).
    sku = bloco.get("data-sku")

    tag_link = bloco.find("a", href=True)
    url_produto = tag_link["href"] if tag_link else None

    tag_nome = bloco.find("h2", class_="title")
    nome = tag_nome.get_text(strip=True) if tag_nome else None

    return sku, nome, url_produto, extrair_precos_bloco(bloco)


async def processar_pagina(sessao, semaforo, cache_detalhes, pagina):
    # Retorna (mensagens, produtos) para imprimir na ordem das páginas
    url = f"{URL_BASE}/medicamentos/?p={pagina}"

    status, texto, erro = await baixar(sessao, semaforo, url, timeout=10)

    if status != 200:
        if erro is not None and status is None:
            return [f"Erro ao acessar a página {pagina}: {erro}"], []
        return [f"Página {pagina} retornou status {status}. Pulando..."], []

    soup = BeautifulSoup(texto, "lxml")

    # Localiza todos os blocos de html que representam um produto
    blocos_produtos = soup.find_all("div", class_="item-product")

    if not blocos_produtos:
        return [f"Nenhum produto encontrado na página {pagina}."], []

    data_coleta = datetime.now().strftime("%d/%m/%Y %H:%M:%S")

    dados_pagina = []
    tarefas_detalhes = []

    for bloco in blocos_produtos:
        sku, nome, url_produto, precos_listagem = extrair_dados_bloco(bloco)
        dados_pagina.append((sku, nome, url_produto, precos_listagem))

        # Um produto repetido em outra página reaproveita a mesma requisição
        if url_produto not in cache_detalhes:
            cache_detalhes[url_produto] = asyncio.ensure_future(
                buscar_dados_da_pagina_produto(sessao, semaforo, url_produto)
            )
        tarefas_detalhes.append(cache_detalhes[url_produto])

    # Solta o HTML já lido (os dados já estão em dados_pagina)
    del soup, blocos_produtos, bloco, texto

    # Busca os dados de todos os produtos ao mesmo tempo, mantendo a ordem
    detalhes = await asyncio.gather(*tarefas_detalhes)

    produtos = []
    for (sku, nome, url_produto, precos_listagem), det in zip(dados_pagina, detalhes):
        det = det or {}  # página do produto falhou: fica só o que veio da listagem

        preco_sem_desconto = det.get("preco_sem_desconto")
        preco_cartao = det.get("preco_cartao")
        if preco_sem_desconto is None and preco_cartao is None:
            # Página do produto falhou ou veio sem preço: usa o card da listagem
            preco_sem_desconto, preco_cartao = precos_listagem
            if preco_cartao is None:
                preco_cartao = preco_sem_desconto
            if preco_sem_desconto is None:
                preco_sem_desconto = preco_cartao
            chave = "preco_listagem" if preco_cartao is not None else "sem_preco"
        else:
            chave = "preco_pagina"
        contadores[chave] += 1

        produtos.append(
            {
                "sku": sku or det.get("sku"),
                "gtin13": det.get("gtin13"),
                "nome": det.get("nome") or nome,
                "marca": det.get("marca"),
                "categorias": det.get("categorias"),
                "preco_sem_desconto": preco_sem_desconto,
                "preco_cartao": preco_cartao,
                "preco_pix": det.get("preco_pix"),
                "em_estoque": det.get("em_estoque"),
                "url": urljoin(URL_BASE, url_produto) if url_produto else None,
                "farmacia": FARMACIA,
                "data_coleta": data_coleta,
            }
        )
    return [], produtos


def salvar_jsonl(lista_produtos):
    # Um produto por linha (JSON Lines): data/raw/veracruz_AAAA-MM-DD.jsonl
    PASTA_RAW.mkdir(parents=True, exist_ok=True)
    arquivo = PASTA_RAW / f"veracruz_{datetime.now():%Y-%m-%d}.jsonl"
    with open(arquivo, "w", encoding="utf-8") as f:
        for produto in lista_produtos:
            linha = {coluna: produto.get(coluna) for coluna in COLUNAS}
            f.write(json.dumps(linha, ensure_ascii=False) + "\n")
    return arquivo


async def main():
    lista_produtos = []
    contador_global = 1
    cache_detalhes = {}
    semaforo = asyncio.Semaphore(MAX_REQUISICOES)
    conector = aiohttp.TCPConnector(limit=0, ttl_dns_cache=300)

    async with aiohttp.ClientSession(headers=headers, connector=conector) as sessao:
        # Dispara todas as páginas de 1 até 224 de uma vez (o semáforo controla
        # quantas requisições rodam juntas) e imprime na ordem das páginas
        tarefas = [
            asyncio.create_task(
                processar_pagina(sessao, semaforo, cache_detalhes, pagina)
            )
            for pagina in range(1, 225)
        ]

        try:
            for tarefa in tarefas:
                mensagens, produtos = await tarefa

                for mensagem in mensagens:
                    print(mensagem)

                for prod_dados in produtos:
                    lista_produtos.append(prod_dados)

                    # Resultados
                    print(f"Produto {contador_global}")
                    print("Nome:", prod_dados["nome"])
                    print("Marca:", prod_dados["marca"])
                    print("Categorias:", prod_dados["categorias"])
                    print("SKU:", prod_dados["sku"])
                    print("GTIN-13:", prod_dados["gtin13"])
                    print("Preço Sem Desconto:", prod_dados["preco_sem_desconto"])
                    print("Preço Cartão:", prod_dados["preco_cartao"])
                    print("Preço Pix:", prod_dados["preco_pix"])
                    print("Em Estoque:", prod_dados["em_estoque"])
                    print("URL:", prod_dados["url"])
                    print("Data Coleta:", prod_dados["data_coleta"])
                    print()

                    contador_global += 1
        finally:
            # Cancela o que sobrou (caso de interrupção) e salva o que foi coletado
            for tarefa in tarefas:
                tarefa.cancel()
            for tarefa in cache_detalhes.values():
                tarefa.cancel()
            arquivo = salvar_jsonl(lista_produtos)

            print("--- Diagnóstico da coleta ---")
            print("Arquivo salvo:", arquivo)
            print("Produtos coletados:", len(lista_produtos))
            print("Preço vindo da página do produto:", contadores["preco_pagina"])
            print("Preço vindo da listagem (página do produto falhou ou sem preço):", contadores["preco_listagem"])
            print("Sem preço em nenhuma das duas fontes:", contadores["sem_preco"])
            print("Páginas do produto que vieram (200) sem preço:", contadores["pagina_sem_preco"])
            print("Páginas do produto que falharam (status/erro -> qtd):", dict(falhas_produto))
            if contadores["pagina_sem_preco"]:
                print(f"HTML das primeiras páginas sem preço salvo em {PASTA_DEBUG}")


if __name__ == "__main__":
    asyncio.run(main())
