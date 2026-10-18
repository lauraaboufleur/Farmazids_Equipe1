import time

import boto3

athena = boto3.client("athena", region_name="us-east-2")

BANCO = "t1_eq1_farmazids"
PASTA_RESULTADOS = "s3://farmazids-t1-equipe1/athena-results/"
WORKGROUP = "primary"
ESPERA_MAXIMA = 50  # segundos; precisa ser menor que o tempo limite da Lambda (60 s)


def lambda_handler(event, context):
    # Entrada: {"sql": "..."}
    # Saída (sempre com os mesmos campos):
    #   {"ok": true/false, "erro": ..., "colunas": [...], "linhas": [[...]], "mb_lidos": ...}
    # Quando ok = false, o erro volta para o Bedrock corrigir o SQL.
    sql = event["sql"]

    # 1. Dispara a consulta
    inicio = athena.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": BANCO},
        ResultConfiguration={"OutputLocation": PASTA_RESULTADOS},
        WorkGroup=WORKGROUP,
    )
    id_consulta = inicio["QueryExecutionId"]

    # 2. Espera terminar, perguntando de segundo em segundo
    comeco = time.time()
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
        time.sleep(1)

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
    resultado = athena.get_query_results(QueryExecutionId=id_consulta)
    colunas = [c["Name"] for c in resultado["ResultSet"]["ResultSetMetadata"]["ColumnInfo"]]
    linhas = []
    for linha in resultado["ResultSet"]["Rows"][1:]:  # a 1ª linha repete o nome das colunas
        linhas.append([celula.get("VarCharValue") for celula in linha["Data"]])

    # Quanto o Athena leu: é por isso que ele cobra (US$ 5 por TB)
    bytes_lidos = execucao["Statistics"].get("DataScannedInBytes", 0)

    return {
        "ok": True,
        "erro": None,
        "colunas": colunas,
        "linhas": linhas,
        "mb_lidos": round(bytes_lidos / 1024 / 1024, 2),
    }
