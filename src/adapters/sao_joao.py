import asyncio
import json
import time
from datetime import datetime

import aiohttp

# ---------------------------------------------------------------------------
# 1. Configurações
# ---------------------------------------------------------------------------

URL_BUSCA = "https://www.saojoaofarmacias.com.br/api/catalog_system/pub/products/search"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
}

# Subcategorias de Medicamentos (id 2001), tiradas de /api/catalog_system/pub/category/tree/2.
# A API só deixa paginar até o produto 2500 de uma mesma busca, e Medicamentos inteiro
# tem ~6900. Por isso a busca é feita por subcategoria (a maior tem ~1400).
# Obs.: a soma das subcategorias dá ~6 produtos a menos que o total de Medicamentos
# (devem estar cadastrados sem subcategoria). Decidimos aceitar essa diferença.
ID_MEDICAMENTOS = 2001
SUBCATEGORIAS = {
    20001: "Diabetes",
    20002: "Cardiológicos",
    20003: "Oftalmologia",
    20004: "Saúde Óssea",
    20005: "Antiparasitários",
    20006: "Pele E Mucosa",
    20007: "Antifúngicos",
    20008: "Saúde Da Mulher",
    20009: "Saúde Do Homem",
    20010: "Nefrológicos",
    20011: "Alergias E Infecções",
    20012: "Gripe E Resfriado",
    20013: "Sistema Nervoso Central",
    20014: "Estômago E Gastrointestinais",
    20015: "Imunossupressores",
    20016: "Anti-inflamatórios",
    20017: "Emagrecer",
    20018: "Reposição Hormonal",
    20019: "Sistema Circulatório",
    20020: "Medicamentos Especiais",
    20021: "Vacinas",
    20022: "Analgésicos",
    20130: "Fitoterápico",
}

FARMACIA = "São João"
POR_PAGINA = 50                   # máximo que a API devolve por chamada
SUBCATEGORIAS_AO_MESMO_TEMPO = 3  # quantas subcategorias são baixadas em paralelo
PAUSA = 0.5                       # segundos entre uma chamada e outra (em cada subcategoria)
TEMPO_LIMITE = 30
TENTATIVAS = 3
STATUS_PARA_REPETIR = (429, 500, 502, 503, 504)
MOSTRAR_CADA_PRODUTO = True       # False = só mostra o total de cada subcategoria


# ---------------------------------------------------------------------------
# 2. Baixar uma página da busca (com novas tentativas se der erro)
# ---------------------------------------------------------------------------

async def baixar_pagina(sessao, id_subcategoria, inicio):
    parametros = {
        "fq": f"C:/{ID_MEDICAMENTOS}/{id_subcategoria}/",
        "_from": inicio,
        "_to": inicio + POR_PAGINA - 1,
    }
    for tentativa in range(1, TENTATIVAS + 1):
        try:
            async with sessao.get(URL_BUSCA, params=parametros) as resposta:
                if resposta.status in STATUS_PARA_REPETIR:
                    raise aiohttp.ClientResponseError(
                        resposta.request_info, resposta.history, status=resposta.status
                    )
                resposta.raise_for_status()
                return await resposta.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as erro:
            if tentativa == TENTATIVAS:
                raise
            espera = 2 ** tentativa   # espera 2 s, depois 4 s...
            print(f"   erro ({erro}), tentando de novo em {espera} s")
            await asyncio.sleep(espera)


# ---------------------------------------------------------------------------
# 3. Transformar o JSON da API nas colunas do nosso padrão (igual à FarmaPonte)
# ---------------------------------------------------------------------------

def preco_ou_vazio(valor):
    # A VTEX devolve preço 0 quando o produto está sem estoque.
    # Preço 0 não é preço de verdade, então guardamos como vazio.
    if not valor:
        return None
    return valor


def montar_registros(produto, data_coleta):
    # categories vem como ["/Medicamentos/Analgésicos/Dor E Febre/", "/Medicamentos/Analgésicos/", ...]
    # O primeiro é o caminho mais completo; viramos uma lista igual à da FarmaPonte.
    caminhos = produto.get("categories") or []
    categorias = caminhos[0].strip("/").split("/") if caminhos else None

    registros = []
    # Um produto pode ter mais de um item (ex.: tamanhos diferentes), cada um com EAN e preço.
    for item in produto.get("items", []):
        vendedores = item.get("sellers") or [{}]
        oferta = vendedores[0].get("commertialOffer", {})

        registros.append({
            "farmacia": FARMACIA,
            "sku": item.get("itemId"),
            "ean": item.get("ean") or None,
            "nome": produto.get("productName"),
            "marca": produto.get("brand"),
            "categorias": categorias,
            # ListPrice é o preço "de". Atenção: o PriceWithoutDiscount da VTEX NÃO é o "de",
            # ele vem igual ao Price (é o preço antes das promoções do carrinho).
            "preco_sem_desconto": preco_ou_vazio(oferta.get("ListPrice")),
            "preco_cartao": preco_ou_vazio(oferta.get("Price")),
            "preco_pix": None,     # a API não informa preço no pix
            "desconto": None,      # o tratamento recalcula o desconto a partir dos preços
            "url": produto.get("link"),
            "data_coleta": data_coleta,
        })
    return registros


# ---------------------------------------------------------------------------
# 4. Coleta: percorre as subcategorias de 50 em 50 produtos
# ---------------------------------------------------------------------------

async def coletar():
    inicio_coleta = time.perf_counter()

    data_coleta = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data_arquivo = datetime.now().strftime("%Y-%m-%d")
    caminho = f"data/raw/sao_joao_{data_arquivo}.jsonl"

    vistos = set()       # skus já salvos (um produto pode aparecer em mais de uma subcategoria)
    contagem = {"salvos": 0, "chamadas": 0}
    falhas = []
    limite = asyncio.Semaphore(SUBCATEGORIAS_AO_MESMO_TEMPO)

    tempo = aiohttp.ClientTimeout(total=TEMPO_LIMITE)
    async with aiohttp.ClientSession(headers=HEADERS, timeout=tempo) as sessao:
        with open(caminho, "w", encoding="utf-8") as arquivo:

            async def coletar_subcategoria(id_subcategoria, nome):
                async with limite:
                    inicio = 0
                    da_subcategoria = 0

                    while True:
                        try:
                            produtos = await baixar_pagina(sessao, id_subcategoria, inicio)
                        except Exception as erro:
                            falhas.append(f"{nome} (_from={inicio}): {erro}")
                            print(f"   FALHOU {nome} a partir de {inicio}: {erro}")
                            break
                        contagem["chamadas"] += 1

                        for produto in produtos:
                            for registro in montar_registros(produto, data_coleta):
                                if registro["sku"] in vistos:
                                    continue
                                vistos.add(registro["sku"])
                                arquivo.write(json.dumps(registro, ensure_ascii=False) + "\n")
                                contagem["salvos"] += 1
                                da_subcategoria += 1
                                if MOSTRAR_CADA_PRODUTO:
                                    print(f"[{contagem['salvos']}] {registro['sku']} {registro['nome']}")

                        await asyncio.sleep(PAUSA)

                        # Se veio menos que uma página cheia, essa subcategoria acabou
                        if len(produtos) < POR_PAGINA:
                            break
                        inicio += POR_PAGINA

                    print(f"=== {nome}: {da_subcategoria} produtos ===")

            await asyncio.gather(*(
                coletar_subcategoria(id_subcategoria, nome)
                for id_subcategoria, nome in SUBCATEGORIAS.items()
            ))

    duracao = time.perf_counter() - inicio_coleta

    print()
    print("Produtos salvos:", contagem["salvos"])
    print("Chamadas à API:", contagem["chamadas"])
    print("Falhas:", len(falhas))
    for falha in falhas:
        print("  ", falha)
    print("Arquivo:", caminho)
    print(f"Tempo total: {duracao:.1f} segundos")


if __name__ == "__main__":
    asyncio.run(coletar())