import requests
import re
import json
from bs4 import BeautifulSoup

headers = {
    "User-Agent": "Mozilla/5.0"
}


def extrair_produto(url):
    resposta = requests.get(url, headers=headers)
    resposta.raise_for_status()

    # 1. JSON oculto: window.dataProduct (nome e preço no cartão)
    padrao = r"window\.dataProduct\s*=\s*"
    encontrado = re.search(padrao, resposta.text)
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


# Teste da função com um produto
if __name__ == "__main__":
    registro = extrair_produto("https://www.farmaponte.com.br/zz-oscillococcinum-30doses-boiron/p")

    for campo, valor in registro.items():
        print(f"{campo:<20} {valor}")

    