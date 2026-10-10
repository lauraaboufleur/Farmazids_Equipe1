import asyncio
import json
import os
import re
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime

import aiohttp

URL_SITE = "https://www.farmaponte.com.br"
URL_LISTA = "https://www.farmaponte.com.br/saude/medicamentos/"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
}

# Quantos pedidos ficam "no ar" ao mesmo tempo (é o que mais mexe no tempo total).
# Teste 40, 60, 80... e pare de subir quando começarem a aparecer erros 429/503.
CONCORRENCIA = 60
# Quantas páginas de listagem podem ser baixadas ao mesmo tempo.
LISTAGEM_SIMULTANEA = 15
TEMPO_LIMITE = 30
TENTATIVAS = 3
MOSTRAR_A_CADA = 200
MOSTRAR_CADA_PRODUTO = True  # False = só mostra o progresso a cada MOSTRAR_A_CADA

PADRAO_LINK = re.compile(r'<h2 class="title">\s*<a href="([^"]+)"')
PADRAO_PRODUTO = re.compile(r"window\.dataProduct\s*=\s*")
PADRAO_LD = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL)
PADRAO_EDRONE = re.compile(r"dataItemEdrone\s*=\s*")
PADRAO_VIRGULA_SOBRANDO = re.compile(r",\s*}")
PADRAO_TIPO_PRODUTO = re.compile(r'"@type"\s*:\s*"Product"')
PADRAO_EAN = re.compile(r'"gtin13"\s*:\s*"(\d+)"')

STATUS_PARA_REPETIR = (429, 500, 502, 503, 504)

medicoes = []
repeticoes = {"total": 0}


# ---------------------------------------------------------------------------
# 1. Download (rede)
# ---------------------------------------------------------------------------

async def baixar(sessao, url):
    for tentativa in range(1, TENTATIVAS + 1):
        try:
            inicio_pedido = time.perf_counter()
            async with sessao.get(url) as resposta:
                status = resposta.status
                conteudo = await resposta.read()
            medicoes.append((time.perf_counter() - inicio_pedido, len(conteudo)))
        except (aiohttp.ClientError, asyncio.TimeoutError):
            if tentativa == TENTATIVAS:
                raise
            repeticoes["total"] += 1
            await asyncio.sleep(2 * tentativa)
            continue

        if status in STATUS_PARA_REPETIR and tentativa < TENTATIVAS:
            repeticoes["total"] += 1
            await asyncio.sleep(2 * tentativa)
            continue

        if status >= 400:
            raise RuntimeError(f"HTTP {status}")
        return conteudo


# ---------------------------------------------------------------------------
# 2. Leitura do HTML (CPU) — roda em outros processos, fora do loop assíncrono
# ---------------------------------------------------------------------------

def ler_produto(conteudo, url):
    html = conteudo.decode("utf-8", errors="replace")

    # 1. JSON oculto: window.dataProduct (nome e preço no cartão)
    encontrado = PADRAO_PRODUTO.search(html)
    if encontrado is None:
        raise ValueError("window.dataProduct não encontrado")
    produto, _ = json.JSONDecoder().raw_decode(html, encontrado.end())

    # 2. JSON-LD: ld+json (EAN)
    dados_ld = {}
    for texto in PADRAO_LD.findall(html):
        try:
            dados = json.loads(texto)
        except json.JSONDecodeError:
            dados = {}
            if PADRAO_TIPO_PRODUTO.search(texto):
                dados["@type"] = "Product"
                ean = PADRAO_EAN.search(texto)
                if ean:
                    dados["gtin13"] = ean.group(1)
        if isinstance(dados, dict) and dados.get("@type") == "Product":
            dados_ld = dados

    # 3. JSON oculto: dataItemEdrone (preço cheio, pix, desconto, marca, categorias)
    #    Antes o código copiava e aplicava re.sub no RESTO INTEIRO da página.
    #    Agora corta só até o fim do <script>, que é onde o JSON termina.
    edrone = {}
    encontrado_edrone = PADRAO_EDRONE.search(html)
    if encontrado_edrone is not None:
        inicio = encontrado_edrone.end()
        fim_script = html.find("</script>", inicio)
        trecho = html[inicio:fim_script] if fim_script != -1 else html[inicio:]
        trecho = PADRAO_VIRGULA_SOBRANDO.sub("}", trecho)
        edrone, _ = json.JSONDecoder().raw_decode(trecho)

    # 4. Juntar tudo em um registro
    marca = edrone.get("brand")
    if not marca:
        marca_ld = (dados_ld.get("brand") or {}).get("name")
        if marca_ld and marca_ld != "None":
            marca = marca_ld

    return {
        "sku": produto.get("sku"),
        "ean": dados_ld.get("gtin13"),
        "nome": produto.get("name"),
        "marca": marca,
        "categorias": edrone.get("categories"),
        "preco_sem_desconto": edrone.get("total_price"),
        "preco_cartao": produto.get("sale_price"),
        "preco_pix": edrone.get("unit_price"),
        "desconto": edrone.get("discount"),
        "url": url,
    }


# ---------------------------------------------------------------------------
# 3. Coleta em "esteira": listagem e produtos andam ao mesmo tempo
# ---------------------------------------------------------------------------

async def coletar(primeira_pagina, ultima_pagina):
    inicio_coleta = time.perf_counter()

    data_coleta = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data_arquivo = datetime.now().strftime("%Y-%m-%d")
    caminho = f"data/raw/farmaponte_{data_arquivo}.jsonl"

    fila = asyncio.Queue()       # links de produto esperando para serem baixados
    vistos = set()               # para não baixar o mesmo produto duas vezes
    limite_listagem = asyncio.Semaphore(LISTAGEM_SIMULTANEA)
    loop = asyncio.get_running_loop()

    contagem = {"feitos": 0, "sucessos": 0}
    falhas = []
    falhas_listagem = []

    conector = aiohttp.TCPConnector(
        limit=CONCORRENCIA,      # máximo de conexões abertas com o site
        limit_per_host=CONCORRENCIA,
        ttl_dns_cache=300,       # resolve o DNS uma vez só
    )
    tempo = aiohttp.ClientTimeout(total=TEMPO_LIMITE)

    with ProcessPoolExecutor(max_workers=os.cpu_count()) as processos, \
            open(caminho, "w", encoding="utf-8") as arquivo:

        async with aiohttp.ClientSession(
            headers=HEADERS, connector=conector, timeout=tempo
        ) as sessao:

            async def ler_listagem(pagina):
                # Baixa uma página da listagem e já joga os links na fila.
                try:
                    async with limite_listagem:
                        conteudo = await baixar(sessao, f"{URL_LISTA}?p={pagina}")
                except Exception as erro:
                    falhas_listagem.append(pagina)
                    print(f"Página {pagina}: ERRO -> {erro}")
                    return
                links = PADRAO_LINK.findall(conteudo.decode("utf-8", errors="replace"))
                if len(links) != 20:
                    print(f"Página {pagina}: {len(links)} produtos")
                for link in links:
                    url = URL_SITE + link
                    if url not in vistos:
                        vistos.add(url)
                        fila.put_nowait(url)

            async def trabalhador():
                # Fica pegando links da fila até a coleta acabar.
                while True:
                    url = await fila.get()
                    mensagem = ""
                    erro_aconteceu = False
                    try:
                        conteudo = await baixar(sessao, url)
                        registro = await loop.run_in_executor(processos, ler_produto, conteudo, url)
                        registro["farmacia"] = "FarmaPonte"
                        registro["data_coleta"] = data_coleta
                        arquivo.write(json.dumps(registro, ensure_ascii=False) + "\n")
                        contagem["sucessos"] += 1
                        mensagem = f"{registro['sku']} {registro['nome']}"
                    except Exception as erro:
                        falhas.append(url)
                        erro_aconteceu = True
                        mensagem = f"ERRO em {url} -> {erro}"
                    finally:
                        contagem["feitos"] += 1
                        numero = contagem["feitos"]
                        # O total vai crescendo enquanto a listagem ainda chega;
                        # depois que ela termina, fica fixo (igual à versão anterior).
                        total = len(vistos)
                        if MOSTRAR_CADA_PRODUTO or erro_aconteceu:
                            print(f"[{numero}/{total}] {mensagem}")
                        if numero % MOSTRAR_A_CADA == 0:
                            print(f"[{numero}/{total}] {time.perf_counter() - inicio_coleta:.1f} s")
                        fila.task_done()

            trabalhadores = [asyncio.create_task(trabalhador()) for _ in range(CONCORRENCIA)]

            # Todas as páginas da listagem começam juntas; os produtos de cada
            # página já começam a ser baixados assim que ela chega.
            paginas = range(primeira_pagina, ultima_pagina + 1)
            await asyncio.gather(*(ler_listagem(p) for p in paginas))
            print(f"Links únicos: {len(vistos)} ({time.perf_counter() - inicio_coleta:.1f} s)")

            await fila.join()          # espera a fila esvaziar
            for t in trabalhadores:
                t.cancel()
            await asyncio.gather(*trabalhadores, return_exceptions=True)

    duracao = time.perf_counter() - inicio_coleta

    print()
    print("Produtos salvos:", contagem["sucessos"])
    print("Falhas:", len(falhas))
    print("Páginas de listagem com erro:", falhas_listagem or "nenhuma")
    print("Arquivo:", caminho)
    print(f"Tempo total: {duracao:.1f} segundos")

    for url in falhas:
        print("  Falhou:", url)

    if medicoes:
        tempos = sorted(m[0] for m in medicoes)
        tamanhos = [m[1] for m in medicoes]
        print()
        print("Pedidos feitos:", len(medicoes))
        print("Pedidos repetidos (erro/429/503):", repeticoes["total"])
        print(f"Pedidos por segundo: {len(medicoes) / duracao:.1f}")
        print(f"Tempo médio por pedido: {sum(tempos) / len(tempos):.2f} s")
        print(f"Tempo do pedido mais lento: {tempos[-1]:.2f} s")
        print(f"90% dos pedidos levaram até: {tempos[int(len(tempos) * 0.9)]:.2f} s")
        print(f"Tamanho médio da página: {sum(tamanhos) / len(tamanhos) / 1024:.0f} KB")


if __name__ == "__main__":
    try:
        import uvloop  # loop de eventos mais rápido no Linux (opcional)
        executar = uvloop.run
    except ImportError:
        executar = asyncio.run

    executar(coletar(1, 271))