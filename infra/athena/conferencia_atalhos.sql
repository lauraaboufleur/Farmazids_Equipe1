-- Conferência dos atalhos da coluna lateral do chatbot.
-- São os mesmos SQL que a gerar-sql usa (src/lambdas/gerar_sql.py, sem Bedrock), com o nome do banco.
-- Rode uma consulta por vez no console do Athena e compare com a tabela do chatbot.
-- Regras comuns: preço de comparação da vw_precos_unitarios (só a coleta mais recente), mesmo
-- EAN sem os zeros à esquerda, e a Farmazids pela vw_precos_unitarios_farmazids.

-- 1. Onde estamos mais caros
--    Pergunta: "Em quais produtos a Farmazids está mais cara que a concorrência?"
--    Até 20 produtos (preço Farmazids <= R$ 500), do maior % acima para o menor. ignorados_acima_200 = quantos
--    ficaram de fora por diferença acima de 200% (provável erro de cadastro).
WITH fz AS (SELECT regexp_replace(trim(CAST(ean AS varchar)), '^0+', '') AS ean_n, min(nome) AS produto, min(TRY_CAST(preco_farmazids AS double)) AS preco
  FROM t1_eq1_farmazids.vw_precos_unitarios_farmazids
  WHERE ean IS NOT NULL
  GROUP BY 1),
conc AS (SELECT regexp_replace(trim(CAST(ean AS varchar)), '^0+', '') AS ean_n, min(nome) AS nome, min(preco_comparacao) AS menor, min_by(farmacia, preco_comparacao) AS farmacia
  FROM t1_eq1_farmazids.vw_precos_unitarios
  WHERE ean IS NOT NULL AND preco_comparacao > 0
  GROUP BY 1),
base AS (SELECT f.produto, f.preco AS farmazids_rs, c.menor AS menor_concorrente_rs, c.farmacia, round(f.preco - c.menor, 2) AS diferenca, round(100.0 * (f.preco - c.menor) / c.menor, 1) AS pct, 100.0 * (f.preco - c.menor) / c.menor AS grandeza
  FROM fz f JOIN conc c ON c.ean_n = f.ean_n
  WHERE f.preco > 0 AND f.preco <= 500 AND f.preco > c.menor)
SELECT produto, farmazids_rs, menor_concorrente_rs, farmacia, diferenca, pct AS acima_pct, (SELECT count(*)
  FROM base
  WHERE grandeza > 200) AS ignorados_acima_200
  FROM base
  WHERE grandeza <= 200
  ORDER BY pct DESC
  LIMIT 20;

-- 2. Onde estamos mais baratos
--    Pergunta: "Em quais produtos a Farmazids está mais barata que a concorrência?"
--    Até 20 produtos, do maior % abaixo para o menor (abaixo_pct = quanto a Farmazids cobra a menos que o
--    menor concorrente). Mesmos limites do 1.
WITH fz AS (SELECT regexp_replace(trim(CAST(ean AS varchar)), '^0+', '') AS ean_n, min(nome) AS produto, min(TRY_CAST(preco_farmazids AS double)) AS preco
  FROM t1_eq1_farmazids.vw_precos_unitarios_farmazids
  WHERE ean IS NOT NULL
  GROUP BY 1),
conc AS (SELECT regexp_replace(trim(CAST(ean AS varchar)), '^0+', '') AS ean_n, min(nome) AS nome, min(preco_comparacao) AS menor, min_by(farmacia, preco_comparacao) AS farmacia
  FROM t1_eq1_farmazids.vw_precos_unitarios
  WHERE ean IS NOT NULL AND preco_comparacao > 0
  GROUP BY 1),
base AS (SELECT f.produto, f.preco AS farmazids_rs, c.menor AS menor_concorrente_rs, c.farmacia, round(c.menor - f.preco, 2) AS diferenca, round(100.0 * (c.menor - f.preco) / c.menor, 1) AS pct, 100.0 * (c.menor - f.preco) / f.preco AS grandeza
  FROM fz f JOIN conc c ON c.ean_n = f.ean_n
  WHERE f.preco > 0 AND f.preco <= 500 AND f.preco < c.menor)
SELECT produto, farmazids_rs, menor_concorrente_rs, farmacia, diferenca, pct AS abaixo_pct, (SELECT count(*)
  FROM base
  WHERE grandeza > 200) AS ignorados_acima_200
  FROM base
  WHERE grandeza <= 200
  ORDER BY pct DESC
  LIMIT 20;

-- 3. Mais vendidos × concorrência
--    Pergunta: "Quais os 10 produtos mais vendidos da Farmazids nos últimos 3 meses e como está o preço deles na concorrência?"
--    Unidades vendidas nos 3 meses até a última venda da base; preço da Farmazids e o menor concorrente
--    pelo mesmo EAN (vazio quando o produto não está na coleta).
WITH ultima AS (SELECT max(date(data_venda)) AS dia
  FROM t1_eq1_farmazids.vw_vendas
  WHERE LOWER(farmacia) = 'farmazids'),
vendas AS (SELECT regexp_replace(trim(CAST(ean_gtin AS varchar)), '^0+', '') AS ean_n, sum(TRY_CAST(quantidade_vendida AS double)) AS unidades
  FROM t1_eq1_farmazids.vw_vendas, ultima
  WHERE LOWER(farmacia) = 'farmazids' AND date(data_venda) > date_add('month', -3, ultima.dia)
  GROUP BY 1),
fz AS (SELECT regexp_replace(trim(CAST(ean AS varchar)), '^0+', '') AS ean_n, min(nome) AS produto, min(TRY_CAST(preco_farmazids AS double)) AS preco
  FROM t1_eq1_farmazids.vw_precos_unitarios_farmazids
  WHERE ean IS NOT NULL
  GROUP BY 1),
conc AS (SELECT regexp_replace(trim(CAST(ean AS varchar)), '^0+', '') AS ean_n, min(nome) AS nome, min(preco_comparacao) AS menor, min_by(farmacia, preco_comparacao) AS farmacia
  FROM t1_eq1_farmazids.vw_precos_unitarios
  WHERE ean IS NOT NULL AND preco_comparacao > 0
  GROUP BY 1)
SELECT COALESCE(f.produto, c.nome, 'Código ' || v.ean_n || ' sem cadastro') AS produto, v.unidades, f.preco AS farmazids_rs, c.menor AS menor_concorrente_rs, c.farmacia, round(100.0 * (f.preco - c.menor) / c.menor, 1) AS diferenca_pct
  FROM vendas v LEFT JOIN fz f ON f.ean_n = v.ean_n LEFT JOIN conc c ON c.ean_n = v.ean_n
  ORDER BY v.unidades DESC
  LIMIT 10;

-- 4. Vendas por mês
--    Pergunta: "Quantas unidades cada farmácia vendeu por mês nos últimos 6 meses?"
--    Unidades por farmácia e mês: o mês da última venda e os 5 anteriores.
WITH ultima AS (SELECT max(date(data_venda)) AS dia
  FROM t1_eq1_farmazids.vw_vendas)
SELECT farmacia, date_format(date(data_venda), '%Y-%m') AS mes, sum(TRY_CAST(quantidade_vendida AS double)) AS unidades
  FROM t1_eq1_farmazids.vw_vendas, ultima
  WHERE date(data_venda) >= date_add('month', -5, date_trunc('month', ultima.dia))
  GROUP BY 1, 2
  ORDER BY 2, 1
  LIMIT 50;

-- 5. Produtos em queda
--    Pergunta: "Quais produtos da Farmazids mais caíram em vendas nos últimos 3 meses?"
--    Unidades dos últimos 3 meses contra os 3 anteriores; só produtos com pelo menos 10 unidades antes.
WITH ultima AS (SELECT max(date(data_venda)) AS dia
  FROM t1_eq1_farmazids.vw_vendas
  WHERE LOWER(farmacia) = 'farmazids'),
itens AS (SELECT regexp_replace(trim(CAST(ean_gtin AS varchar)), '^0+', '') AS ean_n, TRY_CAST(quantidade_vendida AS double) AS q, date(data_venda) AS d
  FROM t1_eq1_farmazids.vw_vendas
  WHERE LOWER(farmacia) = 'farmazids'),
periodos AS (SELECT ean_n, COALESCE(sum(CASE WHEN d > date_add('month', -3, ultima.dia) THEN q END), 0) AS agora, COALESCE(sum(CASE WHEN d <= date_add('month', -3, ultima.dia) AND d > date_add('month', -6, ultima.dia) THEN q END), 0) AS antes
  FROM itens, ultima
  GROUP BY ean_n),
fz AS (SELECT regexp_replace(trim(CAST(ean AS varchar)), '^0+', '') AS ean_n, min(nome) AS produto, min(TRY_CAST(preco_farmazids AS double)) AS preco
  FROM t1_eq1_farmazids.vw_precos_unitarios_farmazids
  WHERE ean IS NOT NULL
  GROUP BY 1),
conc AS (SELECT regexp_replace(trim(CAST(ean AS varchar)), '^0+', '') AS ean_n, min(nome) AS nome, min(preco_comparacao) AS menor, min_by(farmacia, preco_comparacao) AS farmacia
  FROM t1_eq1_farmazids.vw_precos_unitarios
  WHERE ean IS NOT NULL AND preco_comparacao > 0
  GROUP BY 1)
SELECT COALESCE(f.produto, c.nome, 'Código ' || p.ean_n || ' sem cadastro') AS produto, p.antes AS unidades_antes, p.agora AS unidades_agora, round(100.0 * (p.antes - p.agora) / p.antes, 1) AS queda_pct, f.preco AS farmazids_rs, c.menor AS menor_concorrente_rs, c.farmacia
  FROM periodos p LEFT JOIN fz f ON f.ean_n = p.ean_n LEFT JOIN conc c ON c.ean_n = p.ean_n
  WHERE p.antes >= 10 AND p.agora < p.antes
  ORDER BY queda_pct DESC
  LIMIT 15;
