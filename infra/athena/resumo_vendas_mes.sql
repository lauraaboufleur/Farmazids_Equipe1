-- Resumo das vendas por farmácia, EAN e mês (tabela em Parquet), para os atalhos de vendas do chatbot.
-- A base de vendas é fixa (extraída uma vez): rode este CTAS UMA VEZ no console do Athena
-- (banco t1_eq1_farmazids). Ela nunca precisa ser atualizada.
--
-- Colunas:
--   farmacia    como está na vw_vendas
--   ean_n       código de barras sem os zeros à esquerda (o mesmo critério dos preços)
--   mes         primeiro dia do mês (date)
--   unidades    soma de quantidade_vendida
--   vendas      número de vendas (id_venda distintos)
--   ultimo_dia  dia da última venda daquele grupo (para saber se o último mês está completo)
--
-- Na vw_vendas o código de barras se chama ean (a view renomeia o ean_gtin da tabela vendas).
-- Antes de rodar: a pasta do external_location precisa estar vazia (o CTAS não sobrescreve).
-- Se precisar refazer: DROP TABLE t1_eq1_farmazids.resumo_vendas_mes; apague a pasta no S3 e rode de novo.
-- A role da Lambda t1-eq1-executar-athena precisa ler s3://farmazids-t1-equipe1/resumos/ (se a
-- política dela limitar por pasta, acrescente essa).

CREATE TABLE t1_eq1_farmazids.resumo_vendas_mes
WITH (
  format = 'PARQUET',
  write_compression = 'SNAPPY',
  external_location = 's3://farmazids-t1-equipe1/resumos/resumo_vendas_mes/'
) AS
SELECT farmacia,
       regexp_replace(trim(CAST(ean AS varchar)), '^0+', '') AS ean_n,
       date_trunc('month', date(data_venda)) AS mes,
       sum(TRY_CAST(quantidade_vendida AS double)) AS unidades,
       count(DISTINCT id_venda) AS vendas,
       max(date(data_venda)) AS ultimo_dia
FROM t1_eq1_farmazids.vw_vendas
WHERE data_venda IS NOT NULL
GROUP BY 1, 2, 3;

-- Conferência (depois do CTAS): os totais por farmácia devem bater com a vw_vendas.
SELECT r.farmacia, r.unidades, v.unidades AS unidades_vw, r.ultimo_dia, v.ultimo_dia AS ultimo_dia_vw
FROM (SELECT farmacia, sum(unidades) AS unidades, max(ultimo_dia) AS ultimo_dia
      FROM t1_eq1_farmazids.resumo_vendas_mes GROUP BY 1) r
JOIN (SELECT farmacia, sum(TRY_CAST(quantidade_vendida AS double)) AS unidades, max(date(data_venda)) AS ultimo_dia
      FROM t1_eq1_farmazids.vw_vendas WHERE data_venda IS NOT NULL GROUP BY 1) v
  ON v.farmacia = r.farmacia
ORDER BY 1;

-- Tamanho do resumo (deve ser de milhares de linhas, não de milhões).
SELECT count(*) AS linhas, count(DISTINCT ean_n) AS eans, min(mes) AS primeiro_mes, max(mes) AS ultimo_mes
FROM t1_eq1_farmazids.resumo_vendas_mes;
