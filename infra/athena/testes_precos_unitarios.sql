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

-- 9. Marca da FarmaPonte na coleta mais recente: sem marca antes e depois do preenchimento pelo EAN.
--    "antes" = sem marca na coleta (marca_origem diferente de 'coleta'); "depois" = sem marca na view.
WITH ultima AS (
  SELECT max(date(data_coleta)) AS dia FROM t1_eq1_farmazids.vw_precos_concorrentes WHERE lower(farmacia) = 'farmaponte'
)
SELECT count(*) AS produtos,
       count_if(marca_origem IS DISTINCT FROM 'coleta') AS sem_marca_antes,
       count_if(marca_origem = 'ean') AS preenchidas_pelo_ean,
       count_if(marca IS NULL) AS sem_marca_depois
FROM t1_eq1_farmazids.vw_precos_unitarios, ultima
WHERE lower(farmacia) = 'farmaponte' AND date(data_coleta) = ultima.dia;

-- 9b. O "antes" direto na view de origem (deve bater com sem_marca_antes da consulta 9).
SELECT count_if(marca IS NULL OR lower(trim(CAST(marca AS varchar))) IN ('', 'none', 'null', 'nan')) AS sem_marca
FROM t1_eq1_farmazids.vw_precos_concorrentes
WHERE lower(farmacia) = 'farmaponte'
  AND date(data_coleta) = (SELECT max(date(data_coleta)) FROM t1_eq1_farmazids.vw_precos_concorrentes
                           WHERE lower(farmacia) = 'farmaponte');

-- 10. Nenhuma marca existente foi trocada: só a escrita muda ("Ems" -> "EMS").
--     A coluna marcas_trocadas deve vir 0; as linhas mostram as grafias padronizadas.
WITH pares AS (
  SELECT DISTINCT trim(CAST(c.marca AS varchar)) AS antes, u.marca AS depois
  FROM t1_eq1_farmazids.vw_precos_concorrentes c
  JOIN t1_eq1_farmazids.vw_precos_unitarios u
    ON u.farmacia = c.farmacia AND u.sku = c.sku AND u.data_coleta = c.data_coleta
  WHERE u.marca_origem = 'coleta' AND trim(CAST(c.marca AS varchar)) <> u.marca
)
SELECT antes, depois,
       count(*) OVER () AS grafias_padronizadas,
       count_if(regexp_replace(translate(lower(antes), 'áàâãäéèêëíìîïóòôõöúùûüç', 'aaaaaeeeeiiiiooooouuuuc'), '[^a-z0-9]', '')
             <> regexp_replace(translate(lower(depois), 'áàâãäéèêëíìîïóòôõöúùûüç', 'aaaaaeeeeiiiiooooouuuuc'), '[^a-z0-9]', ''))
         OVER () AS marcas_trocadas
FROM pares
ORDER BY depois
LIMIT 100;

-- 11. Exemplos de marca preenchida pelo EAN na FarmaPonte (para conferir a olho).
SELECT nome, marca, ean
FROM t1_eq1_farmazids.vw_precos_unitarios
WHERE lower(farmacia) = 'farmaponte' AND marca_origem = 'ean'
  AND date(data_coleta) = (SELECT max(date(data_coleta)) FROM t1_eq1_farmazids.vw_precos_concorrentes
                           WHERE lower(farmacia) = 'farmaponte')
ORDER BY nome
LIMIT 50;
