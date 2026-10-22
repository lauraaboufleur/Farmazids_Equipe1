import os
import time

import boto3

athena = boto3.client("athena", region_name="us-east-2")

BANCO = "t1_eq1_farmazids"
PASTA_RESULTADOS = "s3://farmazids-t1-equipe1/athena-results/"
WORKGROUP = "primary"
ESPERA_MAXIMA = 50  # segundos; precisa ser menor que o tempo limite da Lambda (60 s)
# Reaproveita o resultado de uma consulta idêntica feita há até tantos minutos (não roda nem cobra
# de novo). Os dados só mudam a cada coleta. "0" desliga.
REUSO_MINUTOS = int(os.environ.get("REUSO_MINUTOS", "60"))
_reuso = {"ligado": REUSO_MINUTOS > 0}


def tempo(etapa, inicio):
    """Log para medir: "[tempo] etapa=athena 1.23 s"."""
    print(f"[tempo] etapa={etapa} {time.perf_counter() - inicio:.2f} s")


def disparar(sql):
    """start_query_execution com reaproveitamento de resultado; se a workgroup não aceitar
    (ex.: motor antigo), segue sem e não tenta mais nesta Lambda."""
    pedido = dict(QueryString=sql, QueryExecutionContext={"Database": BANCO},
                  ResultConfiguration={"OutputLocation": PASTA_RESULTADOS}, WorkGroup=WORKGROUP)
    if _reuso["ligado"]:
        try:
            return athena.start_query_execution(ResultReuseConfiguration={"ResultReuseByAgeConfiguration": {
                "Enabled": True, "MaxAgeInMinutes": REUSO_MINUTOS}}, **pedido)
        except athena.exceptions.InvalidRequestException as erro:
            if "reuse" not in str(erro).lower():
                raise
            print(f"[chatbot] reaproveitamento de resultado recusado, seguindo sem: {erro}")
            _reuso["ligado"] = False
    return athena.start_query_execution(**pedido)


def lambda_handler(event, context):
    # Entrada: {"sql": "..."}
    # Saída (sempre com os mesmos campos):
    #   {"ok": true/false, "erro": ..., "colunas": [...], "linhas": [[...]], "mb_lidos": ...}
    # Quando ok = false, o erro volta para o Bedrock corrigir o SQL.
    sql = event["sql"]
    inicio_total = time.perf_counter()

    # 1. Dispara a consulta
    id_consulta = disparar(sql)["QueryExecutionId"]

    # 2. Espera terminar. Pergunta logo no começo (consulta pequena ou reaproveitada termina rápido)
    #    e vai espaçando até 1 s.
    comeco = time.time()
    intervalo = 0.2
    while True:
        execucao = athena.get_query_execution(QueryExecutionId=id_consulta)["QueryExecution"]
        estado = execucao["Status"]["State"]
        if estado in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        if time.time() - comeco > ESPERA_MAXIMA:
            athena.stop_query_execution(QueryExecutionId=id_consulta)
            return {
                "ok": False,
                "erro": f"A consulta passou de {ESPERA_MAXIMA} s e foi cancelada. "
                        "Faça uma consulta mais simples, filtrando por dt.",
                "colunas": [],
                "linhas": [],
                "mb_lidos": 0,
            }
        time.sleep(intervalo)
        intervalo = min(1.0, intervalo * 1.5)

    tempo("athena", inicio_total)
    estatisticas = execucao.get("Statistics", {})
    print(f"[athena] reaproveitado={estatisticas.get('ResultReuseInformation', {}).get('ReusedPreviousResult')} "
          f"motor={estatisticas.get('EngineExecutionTimeInMillis')} ms "
          f"fila={estatisticas.get('QueryQueueTimeInMillis')} ms")
    if estado != "SUCCEEDED":
        motivo = execucao["Status"].get("StateChangeReason", "motivo não informado")
        return {
            "ok": False,
            "erro": f"O Athena recusou a consulta: {motivo}",
            "colunas": [],
            "linhas": [],
            "mb_lidos": 0,
        }

    # 3. Lê o resultado
    inicio = time.perf_counter()
    resultado = athena.get_query_results(QueryExecutionId=id_consulta)
    colunas = [c["Name"] for c in resultado["ResultSet"]["ResultSetMetadata"]["ColumnInfo"]]
    linhas = []
    for linha in resultado["ResultSet"]["Rows"][1:]:  # a 1ª linha repete o nome das colunas
        linhas.append([celula.get("VarCharValue") for celula in linha["Data"]])

    # Quanto o Athena leu: é por isso que ele cobra (US$ 5 por TB)
    bytes_lidos = execucao["Statistics"].get("DataScannedInBytes", 0)
    tempo("resultados", inicio)
    tempo("total", inicio_total)

    return {
        "ok": True,
        "erro": None,
        "colunas": colunas,
        "linhas": linhas,
        "mb_lidos": round(bytes_lidos / 1024 / 1024, 2),
    }
