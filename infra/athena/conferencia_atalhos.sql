-- Conferência dos atalhos da coluna lateral do chatbot.
-- São os mesmos SQL que a gerar-sql usa (src/lambdas/gerar_sql.py, sem Bedrock), com o nome do banco.
-- Rode uma consulta por vez no console do Athena e compare com a tabela do chatbot.
-- Os de vendas leem o resumo_vendas_mes (crie antes com resumo_vendas_mes.sql).
-- Preços: preço de comparação da vw_precos_unitarios (só a coleta mais recente), mesmo EAN sem os
-- zeros à esquerda; o preço da Farmazids vem da vw_medicamentos.
-- "Últimos 3 meses completos": se a última venda da Farmazids não cai no último dia do mês, esse mês
-- (incompleto) fica de fora e contam os 3 anteriores.

-- 1. Onde estamos mais caros
--    Pergunta: "Em quais produtos a Farmazids está mais cara que a concorrência?"
--    Até 20 produtos (preço Farmazids <= R$ 500), do maior % acima para o menor. ignorados_acima_200 = quantos
--    ficaram de fora por diferença acima de 200% (provável erro de cadastro).
WITH fz AS (SELECT regexp_replace(trim(CAST(ean AS varchar)), '^0+', '') AS ean_n, min(nome) AS produto, min(TRY_CAST(preco_farmazids AS double)) AS preco
  FROM t1_eq1_farmazids.vw_medicamentos
  WHERE ean IS NOT NULL
  GROUP BY 1),
conc AS (SELECT regexp_replace(trim(CAST(ean AS varchar)), '^0+', '') AS ean_n, min(nome) AS nome, min(preco_comparacao) AS menor, min_by(farmacia, preco_comparacao) AS farmacia
  FROM t1_eq1_farmazids.vw_precos_unitarios
  WHERE ean IS NOT NULL AND preco_comparacao > 0
  GROUP BY 1),
base AS (SELECT f.produto, f.preco AS farmazids_rs, c.menor AS menor_concorrente_rs, c.farmacia, round(f.preco - c.menor, 2) AS diferenca, round(100.0 * (f.preco - c.menor) / c.menor, 1) AS pct, 100.0 * (f.preco - c.menor) / c.menor AS grandeza, count_if(100.0 * (f.preco - c.menor) / c.menor > 200) OVER () AS ignorados_acima_200
  FROM fz f JOIN conc c ON c.ean_n = f.ean_n
  WHERE f.preco > 0 AND f.preco <= 500 AND f.preco > c.menor)
SELECT produto, farmazids_rs, menor_concorrente_rs, farmacia, diferenca, pct AS acima_pct, ignorados_acima_200
  FROM base
  WHERE grandeza <= 200
  ORDER BY pct DESC, diferenca DESC
  LIMIT 20;

-- 2. Onde estamos mais baratos
--    Pergunta: "Em quais produtos a Farmazids está mais barata que a concorrência?"
--    Até 20 produtos, do maior % abaixo para o menor (abaixo_pct = quanto a Farmazids cobra a menos que o
--    menor concorrente). Mesmos limites do 1.
WITH fz AS (SELECT regexp_replace(trim(CAST(ean AS varchar)), '^0+', '') AS ean_n, min(nome) AS produto, min(TRY_CAST(preco_farmazids AS double)) AS preco
  FROM t1_eq1_farmazids.vw_medicamentos
  WHERE ean IS NOT NULL
  GROUP BY 1),
conc AS (SELECT regexp_replace(trim(CAST(ean AS varchar)), '^0+', '') AS ean_n, min(nome) AS nome, min(preco_comparacao) AS menor, min_by(farmacia, preco_comparacao) AS farmacia
  FROM t1_eq1_farmazids.vw_precos_unitarios
  WHERE ean IS NOT NULL AND preco_comparacao > 0
  GROUP BY 1),
base AS (SELECT f.produto, f.preco AS farmazids_rs, c.menor AS menor_concorrente_rs, c.farmacia, round(c.menor - f.preco, 2) AS diferenca, round(100.0 * (c.menor - f.preco) / c.menor, 1) AS pct, 100.0 * (c.menor - f.preco) / f.preco AS grandeza, count_if(100.0 * (c.menor - f.preco) / f.preco > 200) OVER () AS ignorados_acima_200
  FROM fz f JOIN conc c ON c.ean_n = f.ean_n
  WHERE f.preco > 0 AND f.preco <= 500 AND f.preco < c.menor)
SELECT produto, farmazids_rs, menor_concorrente_rs, farmacia, diferenca, pct AS abaixo_pct, ignorados_acima_200
  FROM base
  WHERE grandeza <= 200
  ORDER BY pct DESC, diferenca DESC
  LIMIT 20;

-- 3. Mais vendidos × concorrência
--    Pergunta: "Quais os 10 produtos mais vendidos da Farmazids nos últimos 3 meses e como está o preço deles na concorrência?"
--    Unidades nos últimos 3 meses completos (resumo_vendas_mes); só os 10 do topo são cruzados com os preços
--    (vazio quando o produto não está na coleta).
WITH ultima AS (SELECT max(ultimo_dia) AS dia
  FROM t1_eq1_farmazids.resumo_vendas_mes
  WHERE LOWER(farmacia) = 'farmazids'),
fim AS (SELECT CASE WHEN dia = last_day_of_month(dia) THEN date_trunc('month', dia) ELSE date_add('month', -1, date_trunc('month', dia)) END AS mes
  FROM ultima),
mais AS (SELECT r.ean_n, sum(r.unidades) AS unidades
  FROM t1_eq1_farmazids.resumo_vendas_mes r, fim
  WHERE LOWER(r.farmacia) = 'farmazids' AND r.ean_n IS NOT NULL AND r.ean_n <> '' AND r.mes > date_add('month', -3, fim.mes) AND r.mes <= fim.mes
  GROUP BY 1
  ORDER BY 2 DESC, 1
  LIMIT 10),
fz AS (SELECT regexp_replace(trim(CAST(ean AS varchar)), '^0+', '') AS ean_n, min(nome) AS produto, min(TRY_CAST(preco_farmazids AS double)) AS preco
  FROM t1_eq1_farmazids.vw_medicamentos
  WHERE ean IS NOT NULL AND regexp_replace(trim(CAST(ean AS varchar)), '^0+', '') IN (SELECT ean_n
  FROM mais)
  GROUP BY 1),
conc AS (SELECT regexp_replace(trim(CAST(ean AS varchar)), '^0+', '') AS ean_n, min(nome) AS nome, min(preco_comparacao) AS menor, min_by(farmacia, preco_comparacao) AS farmacia
  FROM t1_eq1_farmazids.vw_precos_unitarios
  WHERE ean IS NOT NULL AND preco_comparacao > 0 AND regexp_replace(trim(CAST(ean AS varchar)), '^0+', '') IN (SELECT ean_n
  FROM mais)
  GROUP BY 1)
SELECT COALESCE(f.produto, c.nome, 'Código ' || m.ean_n || ' sem cadastro') AS produto, m.unidades, f.preco AS farmazids_rs, c.menor AS menor_concorrente_rs, c.farmacia, round(100.0 * (f.preco - c.menor) / c.menor, 1) AS diferenca_pct
  FROM mais m LEFT JOIN fz f ON f.ean_n = m.ean_n LEFT JOIN conc c ON c.ean_n = m.ean_n
  ORDER BY m.unidades DESC, m.ean_n
  LIMIT 10;

-- 4. Vendas por mês
--    Pergunta: "Quantas unidades cada farmácia vendeu por mês nos últimos 6 meses?"
--    Unidades por farmácia e mês: o mês da última venda e os 5 anteriores.
WITH ultima AS (SELECT max(mes) AS mes
  FROM t1_eq1_farmazids.resumo_vendas_mes)
SELECT r.farmacia, date_format(r.mes, '%Y-%m') AS mes, sum(r.unidades) AS unidades
  FROM t1_eq1_farmazids.resumo_vendas_mes r, ultima
  WHERE r.mes >= date_add('month', -5, ultima.mes)
  GROUP BY 1, 2
  ORDER BY 2, 1
  LIMIT 50;

-- 5. Produtos em queda
--    Pergunta: "Quais produtos da Farmazids mais caíram em vendas nos últimos 3 meses?"
--    Últimos 3 meses completos contra os 3 anteriores; só produtos com pelo menos 10 unidades antes; as 15
--    maiores quedas são cruzadas com os preços.
WITH ultima AS (SELECT max(ultimo_dia) AS dia
  FROM t1_eq1_farmazids.resumo_vendas_mes
  WHERE LOWER(farmacia) = 'farmazids'),
fim AS (SELECT CASE WHEN dia = last_day_of_month(dia) THEN date_trunc('month', dia) ELSE date_add('month', -1, date_trunc('month', dia)) END AS mes
  FROM ultima),
periodos AS (SELECT r.ean_n, COALESCE(sum(CASE WHEN r.mes > date_add('month', -3, fim.mes) THEN r.unidades END), 0) AS agora, COALESCE(sum(CASE WHEN r.mes <= date_add('month', -3, fim.mes) THEN r.unidades END), 0) AS antes
  FROM t1_eq1_farmazids.resumo_vendas_mes r, fim
  WHERE LOWER(r.farmacia) = 'farmazids' AND r.ean_n IS NOT NULL AND r.ean_n <> '' AND r.mes > date_add('month', -6, fim.mes) AND r.mes <= fim.mes
  GROUP BY 1),
queda AS (SELECT ean_n, antes, agora, round(100.0 * (antes - agora) / antes, 1) AS queda_pct
  FROM periodos
  WHERE antes >= 10 AND agora < antes
  ORDER BY 4 DESC, 2 DESC
  LIMIT 15),
fz AS (SELECT regexp_replace(trim(CAST(ean AS varchar)), '^0+', '') AS ean_n, min(nome) AS produto, min(TRY_CAST(preco_farmazids AS double)) AS preco
  FROM t1_eq1_farmazids.vw_medicamentos
  WHERE ean IS NOT NULL AND regexp_replace(trim(CAST(ean AS varchar)), '^0+', '') IN (SELECT ean_n
  FROM queda)
  GROUP BY 1),
conc AS (SELECT regexp_replace(trim(CAST(ean AS varchar)), '^0+', '') AS ean_n, min(nome) AS nome, min(preco_comparacao) AS menor, min_by(farmacia, preco_comparacao) AS farmacia
  FROM t1_eq1_farmazids.vw_precos_unitarios
  WHERE ean IS NOT NULL AND preco_comparacao > 0 AND regexp_replace(trim(CAST(ean AS varchar)), '^0+', '') IN (SELECT ean_n
  FROM queda)
  GROUP BY 1)
SELECT COALESCE(f.produto, c.nome, 'Código ' || q.ean_n || ' sem cadastro') AS produto, q.antes AS unidades_antes, q.agora AS unidades_agora, q.queda_pct, f.preco AS farmazids_rs, c.menor AS menor_concorrente_rs, c.farmacia
  FROM queda q LEFT JOIN fz f ON f.ean_n = q.ean_n LEFT JOIN conc c ON c.ean_n = q.ean_n
  ORDER BY q.queda_pct DESC, q.antes DESC
  LIMIT 15;
