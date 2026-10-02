import requests
import re
import json
import time
from datetime import datetime
from bs4 import BeautifulSoup

URL_SITE = "https://www.farmaponte.com.br"
URL_LISTA = "https://www.farmaponte.com.br/saude/medicamentos/"

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}


def extrair_produto(url):
    resposta = requests.get(url, headers=HEADERS)
    resposta.raise_for_status()

    # 1. JSON oculto: window.dataProduct (nome e preço no cartão)
    padrao = r"window\.dataProduct\s*=\s*"
    encontrado = re.search(padrao, resposta.text)
    if encontrado is None:
        raise ValueError("window.dataProduct não encontrado")
    inicio = encontrado.end()
    produto, fim = json.JSONDecoder().raw_decode(resposta.text, inicio)

    # 2. JSON-LD: ld+json (EAN)
    soup = BeautifulSoup(resposta.text, "lxml")
    dados_ld = {}
    for bloco in soup.find_all("script", type="application/ld+json"):
        texto = bloco.string
        try:
            dados = json.loads(texto)
        except json.JSONDecodeError:
            dados = {}
            if re.search(r'"@type"\s*:\s*"Product"', texto):
                dados["@type"] = "Product"
                ean = re.search(r'"gtin13"\s*:\s*"(\d+)"', texto)
                if ean:
                    dados["gtin13"] = ean.group(1)
        if dados.get("@type") == "Product":
            dados_ld = dados

    # 3. JSON oculto: dataItemEdrone (preço cheio, pix, desconto, marca)
    padrao_edrone = r"dataItemEdrone\s*=\s*"
    encontrado_edrone = re.search(padrao_edrone, resposta.text)
    if encontrado_edrone is None:
        raise ValueError("dataItemEdrone não encontrado")
    trecho = resposta.text[encontrado_edrone.end():]
    trecho = re.sub(r",\s*}", "}", trecho)
    edrone, fim = json.JSONDecoder().raw_decode(trecho)

    # 4. Juntar tudo em um registro
    marca = edrone.get("brand")
    if not marca:
        marca_ld = dados_ld.get("brand", {}).get("name")
        if marca_ld and marca_ld != "None":
            marca = marca_ld

    registro = {
        "sku": produto.get("sku"),
        "ean": dados_ld.get("gtin13"),
        "nome": produto.get("name"),
        "marca": marca,
        "preco_sem_desconto": edrone.get("total_price"),
        "preco_cartao": produto.get("price"),
        "preco_pix": edrone.get("unit_price"),
        "desconto": edrone.get("discount"),
        "url": url,
    }

    return registro


def extrair_links(primeira_pagina, ultima_pagina):
    links = []

    for pagina in range(primeira_pagina, ultima_pagina + 1):
        url = f"{URL_LISTA}?p={pagina}"
        resposta = requests.get(url, headers=HEADERS)

        soup = BeautifulSoup(resposta.text, "lxml")
        produtos = soup.find_all("div", class_="item-product")

        print(f"Página {pagina}: status {resposta.status_code}, {len(produtos)} produtos")

        if len(produtos) == 0:
            break

        for item in produtos:
            link = item.find("h2", class_="title").find("a")["href"]
            links.append(URL_SITE + link)

        time.sleep(1)

    links = list(dict.fromkeys(links))
    return links


def coletar(primeira_pagina, ultima_pagina):
    links = extrair_links(primeira_pagina, ultima_pagina)
    print()
    print("Links únicos:", len(links))
    print()

    data_coleta = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data_arquivo = datetime.now().strftime("%Y-%m-%d")
    caminho = f"data/raw/farmaponte_{data_arquivo}.jsonl"

    total = len(links)
    sucessos = 0
    falhas = []

    with open(caminho, "w", encoding="utf-8") as arquivo:
        for numero, link in enumerate(links, start=1):
            try:
                registro = extrair_produto(link)
                registro["farmacia"] = "FarmaPonte"
                registro["data_coleta"] = data_coleta

                arquivo.write(json.dumps(registro, ensure_ascii=False) + "\n")
                arquivo.flush()

                sucessos += 1
                print(f"[{numero}/{total}] {registro['sku']} {registro['nome']}")
            except Exception as erro:
                falhas.append(link)
                print(f"[{numero}/{total}] ERRO em {link} -> {erro}")
            time.sleep(1)

    print()
    print("Produtos salvos:", sucessos)
    print("Falhas:", len(falhas))
    print("Arquivo:", caminho)

    for link in falhas:
        print("  Falhou:", link)


if __name__ == "__main__":
    coletar(1, 3) #paginas do site que serão coletadas
#com 271 paginas dura ~3horas
