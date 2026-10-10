import requests
import time
import json
from datetime import datetime
from bs4 import BeautifulSoup
from teste_pagina import extrair_produto

url_base = "https://www.farmaponte.com.br/saude/medicamentos/"
primeira_pagina = 1
ultima_pagina = 3

headers = {
    "User-Agent": "Mozilla/5.0"
}

# 1. Coletar os links de todas as páginas
links = []

for pagina in range(primeira_pagina, ultima_pagina + 1):
    url = f"{url_base}?p={pagina}"
    resposta = requests.get(url, headers=headers)

    soup = BeautifulSoup(resposta.text, "lxml")
    produtos = soup.find_all("div", class_="item-product")

    print(f"Página {pagina}: status {resposta.status_code}, {len(produtos)} produtos")

    if len(produtos) == 0:
        break

    for item in produtos:
        link = item.find("h2", class_="title").find("a")["href"]
        links.append("https://www.farmaponte.com.br" + link)

    time.sleep(1)

links = list(dict.fromkeys(links))
print()
print("Links únicos:", len(links))
print()

# 2. Extrair cada produto e salvar na hora
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