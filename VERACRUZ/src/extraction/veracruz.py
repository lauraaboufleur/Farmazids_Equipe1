import asyncio
import re
from datetime import datetime
import aiohttp
import pandas as pd
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
ARQUIVO_SAIDA = "produtos_drogaria_veracruz.parquet"
 
 
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
 
 
def limpar_desconto(texto):
    # Converte 67%off (ou 67% off) em número: 67.
    if not texto:
        return None
 
    achado = re.search(r"\d+(?:[.,]\d+)?", texto)
    if not achado:
        return None
 
    valor = float(achado.group().replace(",", "."))
    return int(valor) if valor.is_integer() else valor
 
 
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
 
 
async def buscar_gtin_e_marca_da_pagina_produto(sessao, semaforo, url_relativa):
    # Acessa a página individual do produto uma vez e extrai GTIN-13 e Marca.
    if not url_relativa:
        return None, None
 
    status, texto, _ = await baixar(
        sessao, semaforo, f"{URL_BASE}{url_relativa}", timeout=10
    )
    if status != 200:
        return None, None
 
    match_gtin = re.search(r'"gtin13"\s*:\s*"(\d+)"', texto)
    match_marca = re.search(
        r'"brand"\s*:\s*\{[^}]*?"name"\s*:\s*"([^"]+)"', texto
    )
    gtin = match_gtin.group(1) if match_gtin else None
    marca = match_marca.group(1) if match_marca else None
    return gtin, marca
 
 
def extrair_dados_bloco(bloco):
    # SKU
    sku = bloco.get("data-sku")
 
    # URL para buscar o GTIN e a Marca na página individual
    tag_link = bloco.find("a", href=True)
    url_produto = tag_link["href"] if tag_link else None
 
    # Nome
    tag_nome = bloco.find("h2", class_="title")
    nome = tag_nome.get_text(strip=True) if tag_nome else None
 
    # Os preços ficam dentro de div.box-prices. Buscar só ali evita pegar
    # o preço Pix (<p class="seal-pix ... sale-price">), que também tem
    # a classe "sale-price" e aparece antes no HTML.
    box_precos = bloco.find("div", class_="box-prices") or bloco
 
    # Preço Original (De:)
    tag_preco_orig = box_precos.find("p", class_="unit-price")
    preco_original = limpar_preco(
        tag_preco_orig.get_text(strip=True) if tag_preco_orig else None
    )
 
    # Preço com Desconto (Por:)
    tag_preco_desc = box_precos.find("p", class_="sale-price")
 
    # Caso seja produto PBM / Desconto Laboratório
    if not tag_preco_desc:
        tag_preco_desc = bloco.find("div", class_="pbm-prices")
 
    # Tenta capturar direto do input do formulário se ambos falharem
    if tag_preco_desc:
        preco_desconto = tag_preco_desc.get_text(" ", strip=True)

        # Limpa textos extras como "A partir de:" ficando apenas o valor
        if "A partir de:" in preco_desconto:
            preco_desconto = preco_desconto.replace("A partir de:", "").strip()
    else:
        input_price = bloco.find("input", {"name": "price"})
        preco_desconto = (
            f"R$ {input_price.get('value')}" if input_price else None
        )
    preco_desconto = limpar_preco(preco_desconto)
 
    tag_desconto = bloco.find("span", class_="descont")
    if not tag_desconto:
        tag_desconto = bloco.find("span", class_=re.compile(r"pbm-seal", re.I))
 
    desconto = limpar_desconto(
        tag_desconto.get_text(strip=True) if tag_desconto else None
    )

    # Sem desconto (zero ou ausente)
    if not desconto:
        preco_original = preco_desconto
        preco_desconto = None

    dados = {
        "nome": nome,
        "sku": sku,
        "preco_original": preco_original,
        "preco_desconto": preco_desconto,
        "desconto": desconto,
    }
    return dados, url_produto
 
 
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
        dados, url_produto = extrair_dados_bloco(bloco)
        dados_pagina.append(dados)
 
        # Um produto repetido em outra página reaproveita a mesma requisição
        if url_produto not in cache_detalhes:
            cache_detalhes[url_produto] = asyncio.ensure_future(
                buscar_gtin_e_marca_da_pagina_produto(
                    sessao, semaforo, url_produto
                )
            )
        tarefas_detalhes.append(cache_detalhes[url_produto])
 
    # Busca GTIN-13 e Marca de todos os produtos ao mesmo tempo, mantendo a ordem
    detalhes = await asyncio.gather(*tarefas_detalhes)
 
    produtos = []
    for dados, (gtin, marca) in zip(dados_pagina, detalhes):
        produtos.append(
            {
                "nome": dados["nome"],
                "marca": marca,
                "sku": dados["sku"],
                "gtin13": gtin,
                "preco_original": dados["preco_original"],
                "preco_desconto": dados["preco_desconto"],
                "desconto": dados["desconto"],
                "data_coleta": data_coleta,
            }
        )
    return [], produtos
 
 
def salvar_parquet(lista_produtos):
    colunas = [
        "nome",
        "marca",
        "sku",
        "gtin13",
        "preco_original",
        "preco_desconto",
        "desconto",
        "data_coleta",
    ]
    df = pd.DataFrame(lista_produtos, columns=colunas)
    df["preco_original"] = df["preco_original"].astype("float64")
    df["preco_desconto"] = df["preco_desconto"].astype("float64")
    df["desconto"] = df["desconto"].astype("float64")
    df.to_parquet(ARQUIVO_SAIDA, index=False, engine="pyarrow")
 
 
async def main():
    lista_produtos = []
    contador_global = 1
    cache_detalhes = {}
    semaforo = asyncio.Semaphore(MAX_REQUISICOES)
    conector = aiohttp.TCPConnector(limit=0, ttl_dns_cache=300)
 
    async with aiohttp.ClientSession(headers=headers, connector=conector) as sessao:
        # Dispara todas as páginas de 1 até 225 de uma vez (o semáforo controla
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
                    print("SKU:", prod_dados["sku"])
                    print("GTIN-13:", prod_dados["gtin13"])
                    print("Preço Original:", prod_dados["preco_original"])
                    print("Preço Desconto:", prod_dados["preco_desconto"])
                    print("Desconto(em %):", prod_dados["desconto"])
                    print("Data Coleta:", prod_dados["data_coleta"])
                    print()
 
                    contador_global += 1
        finally:
            # Cancela o que sobrou (caso de interrupção) e salva o que foi coletado
            for tarefa in tarefas:
                tarefa.cancel()
            for tarefa in cache_detalhes.values():
                tarefa.cancel()
            salvar_parquet(lista_produtos)
 
 
if __name__ == "__main__":
    asyncio.run(main())