-- Testes das views vw_precos_unitarios e vw_precos_unitarios_farmazids.
-- Rode uma consulta por vez no console do Athena, depois de criar as duas views
-- (vw_precos_unitarios.sql e vw_precos_unitarios_farmazids.sql).

-- 1. Casos conhecidos: rode o teste_casos_conhecidos.sql (não precisa das views).
--    Ele passa 24 nomes reais pelo mesmo SQL das views; a coluna confere deve vir true em todos.

-- 2. Cobertura por farmácia na coleta mais recente: % com quantidade identificada.
--    Esperado (pelo teste com os nomes coletados): 97% a 99% dos produtos.
SELECT farmacia,
       count(*) AS produtos,
       round(100.0 * count(quantidade) / count(*), 1) AS pct_com_quantidade,
       round(100.0 * count_if(concentracao IS NOT NULL AND quantidade IS NOT NULL)
             / nullif(count_if(concentracao IS NOT NULL), 0), 1) AS pct_medicamentos_com_quantidade
FROM t1_eq1_farmazids.vw_precos_unitarios
WHERE date(data_coleta) = (SELECT max(date(data_coleta)) FROM t1_eq1_farmazids.vw_precos_concorrentes)
GROUP BY farmacia
ORDER BY farmacia;

-- 3. Conferência a olho: dipirona na coleta mais recente, do menor preço por unidade ao maior.
--    Conferir: "1g 10 comprimidos" -> 10 comprimido | 1000mg; "500mg/ml 20ml" -> 20 ml | 500mg/ml.
SELECT farmacia, nome, preco_pix, quantidade, unidade, concentracao, forma, preco_pix_por_unidade
FROM t1_eq1_farmazids.vw_precos_unitarios
WHERE lower(nome) LIKE '%dipirona%'
  AND date(data_coleta) = (SELECT max(date(data_coleta)) FROM t1_eq1_farmazids.vw_precos_concorrentes)
ORDER BY concentracao, forma, unidade, preco_pix_por_unidade
LIMIT 100;

-- 4. Comparação justa: menor preço por comprimido de cada farmácia, só entre equivalentes
--    (mesma concentração, forma e unidade). É o formato que a gerar-sql vai usar.
SELECT concentracao, forma, unidade, farmacia,
       min(preco_pix_por_unidade) AS menor_preco_pix_por_unidade,
       min_by(nome, preco_pix_por_unidade) AS produto
FROM t1_eq1_farmazids.vw_precos_unitarios
WHERE lower(nome) LIKE '%dipirona%'
  AND forma = 'comprimido'
  AND preco_pix_por_unidade IS NOT NULL
  AND date(data_coleta) = (SELECT max(date(data_coleta)) FROM t1_eq1_farmazids.vw_precos_concorrentes)
GROUP BY concentracao, forma, unidade, farmacia
ORDER BY concentracao, menor_preco_pix_por_unidade;

-- 5. Farmazids x concorrentes pelo EAN (mesmo produto, mesma embalagem).
SELECT f.nome, f.preco_farmazids, f.preco_farmazids_por_unidade,
       c.farmacia, c.preco_pix, c.preco_pix_por_unidade, f.unidade
FROM t1_eq1_farmazids.vw_precos_unitarios_farmazids f
JOIN t1_eq1_farmazids.vw_precos_unitarios c ON c.ean = f.ean
WHERE date(c.data_coleta) = (SELECT max(date(data_coleta)) FROM t1_eq1_farmazids.vw_precos_concorrentes)
ORDER BY f.nome, c.preco_pix_por_unidade
LIMIT 100;

-- 6. Farmazids x concorrentes sem EAN em comum: por equivalência (concentração, forma e unidade),
--    sempre dentro do mesmo remédio (o filtro pelo nome).
SELECT 'farmazids' AS farmacia, nome, preco_farmazids AS preco, preco_farmazids_por_unidade AS preco_por_unidade,
       concentracao, forma, unidade
FROM t1_eq1_farmazids.vw_precos_unitarios_farmazids
WHERE lower(nome) LIKE '%sildenafila%'
UNION ALL
SELECT farmacia, nome, preco_pix, preco_pix_por_unidade, concentracao, forma, unidade
FROM t1_eq1_farmazids.vw_precos_unitarios
WHERE lower(nome) LIKE '%sildenafila%'
  AND date(data_coleta) = (SELECT max(date(data_coleta)) FROM t1_eq1_farmazids.vw_precos_concorrentes)
ORDER BY concentracao, forma, unidade, preco_por_unidade;

-- 7. Exemplos sem quantidade (devem ser nomes que não dizem o tamanho da embalagem).
SELECT farmacia, nome
FROM t1_eq1_farmazids.vw_precos_unitarios
WHERE quantidade IS NULL AND concentracao IS NOT NULL
  AND date(data_coleta) = (SELECT max(date(data_coleta)) FROM t1_eq1_farmazids.vw_precos_concorrentes)
LIMIT 30;

-- 8. Preço de comparação (Pix; se vazio, cartão), na coleta mais recente.
--    Esperado: São João com 100% 'cartao'; as outras quase tudo 'pix'; sem_preco perto de 0.
SELECT farmacia,
       count(*) AS produtos,
       round(100.0 * count_if(preco_comparacao_origem = 'pix') / count(*), 1) AS pct_pix,
       round(100.0 * count_if(preco_comparacao_origem = 'cartao') / count(*), 1) AS pct_cartao,
       count_if(preco_comparacao IS NULL) AS sem_preco,
       round(100.0 * count(preco_comparacao_por_unidade) / count(*), 1) AS pct_com_preco_por_unidade
FROM t1_eq1_farmazids.vw_precos_unitarios
WHERE date(data_coleta) = (SELECT max(date(data_coleta)) FROM t1_eq1_farmazids.vw_precos_concorrentes)
GROUP BY farmacia
ORDER BY farmacia;
