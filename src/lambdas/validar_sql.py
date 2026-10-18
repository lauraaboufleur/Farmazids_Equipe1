import re

# Regras de validação do SQL gerado pelo Bedrock (slide 6 da aula de IA).
TABELAS_PERMITIDAS = {"vw_precos_concorrentes", "vw_vendas", "vw_medicamentos"}
LIMITE_MAXIMO = 50
PALAVRAS_PROIBIDAS = [
    "insert", "update", "delete", "drop", "alter", "create", "merge",
    "truncate", "grant", "revoke", "unload", "msck", "call", "execute",
]


def validar(sql):
    """Devolve None se o SQL estiver ok, ou um texto explicando o problema.
    O texto volta para o Bedrock, então precisa dizer o que corrigir."""
    if not sql or not sql.strip():
        return "O SQL veio vazio."

    texto = sql.strip().rstrip(";").strip()
    minusculo = texto.lower()

    # R1: uma consulta só
    if ";" in texto:
        return "Envie apenas uma consulta, sem ';' no meio."

    # R2: só leitura
    if not re.match(r"^(select|with)\b", minusculo):
        return "A consulta precisa começar com SELECT."
    for palavra in PALAVRAS_PROIBIDAS:
        if re.search(rf"\b{palavra}\b", minusculo):
            return f"Comando não permitido: {palavra.upper()}. Use apenas SELECT."

    # R3: só as views do projeto
    # Nomes criados com WITH (ex.: "WITH ultimos AS (...)") também são aceitos.
    nomes_with = set(re.findall(r"\b(\w+)\s+as\s*\(", minusculo))
    # Pega o nome depois de FROM ou JOIN, ignorando funções como UNNEST(...)
    tabelas = re.findall(r"\b(?:from|join)\s+([\"a-z_][\w.\"]*)(?![\w.\"])(?!\s*\()", minusculo)
    for tabela in tabelas:
        nome = tabela.split(".")[-1].strip('"')
        if nome not in TABELAS_PERMITIDAS and nome not in nomes_with:
            permitidas = ", ".join(sorted(TABELAS_PERMITIDAS))
            return f"Tabela não permitida: {nome}. Use apenas: {permitidas}."

    # R4: LIMIT obrigatório no final, até LIMITE_MAXIMO linhas
    limite = re.search(r"\blimit\s+(\d+)\s*$", minusculo)
    if not limite:
        return f"A consulta precisa terminar com LIMIT de no máximo {LIMITE_MAXIMO}."
    if int(limite.group(1)) > LIMITE_MAXIMO:
        return f"O LIMIT pode ser no máximo {LIMITE_MAXIMO}."

    return None


def lambda_handler(event, context):
    # Entrada: {"sql": "..."}  ->  Saída: {"valido": true/false, "erro": "..."}
    erro = validar(event.get("sql", ""))
    return {"valido": erro is None, "erro": erro}
