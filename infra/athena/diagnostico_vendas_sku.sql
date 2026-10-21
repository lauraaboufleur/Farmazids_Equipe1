-- Diagnóstico: vendas com SKU sem cadastro (feedback do cliente: FarmaPonte, janeiro de 2026,
-- 13 vendas de dipirona e só R$ 20 arrecadados; só 1 das 13 tinha SKU cadastrado).
-- Rode uma consulta por vez no console do Athena (banco t1_eq1_farmazids). Nada aqui altera dados.
--
-- A vw_vendas e a vw_medicamentos foram criadas no console e não estão no repositório. Por isso:
--   1. Rode o PASSO 0 e confira os nomes das colunas.
--   2. Se algum nome for diferente do que está nos blocos "vendas" e "cadastro" das consultas
--      A, B e C, troque só nesses blocos (o resto das consultas usa os nomes padronizados).
-- A farmácia é comparada sem acento, espaço, sublinhado nem "Drogaria" ("São João", "sao_joao" e
-- "Sao Joao" viram saojoao; "Drogaria São Paulo" vira saopaulo).
-- O preço vem do cadastro (a coleta mais recente), não do dia da venda.
-- O cadastro de produtos com SKU é a vw_precos_concorrentes (farmacia + sku + nome + preços,
-- vindos da coleta nos sites). A vw_medicamentos (tabela da Farmazids) não tem SKU, só EAN.

-- =====================================================================================
-- PASSO 0. Definição das views e colunas (rode cada linha separada)
-- =====================================================================================
SHOW CREATE VIEW t1_eq1_farmazids.vw_vendas;
SHOW CREATE VIEW t1_eq1_farmazids.vw_medicamentos;
SHOW CREATE VIEW t1_eq1_farmazids.vw_precos_concorrentes;
DESCRIBE t1_eq1_farmazids.vw_vendas;
SELECT * FROM t1_eq1_farmazids.vw_vendas LIMIT 20;
-- Como a farmácia e o SKU estão escritos em cada lado (diferença de grafia quebra o cruzamento):
SELECT 'vendas' AS origem, farmacia, count(*) AS linhas, min(CAST(sku AS varchar)) AS exemplo_sku
FROM t1_eq1_farmazids.vw_vendas GROUP BY farmacia
UNION ALL
SELECT 'cadastro', farmacia, count(*), min(CAST(sku AS varchar))
FROM t1_eq1_farmazids.vw_precos_concorrentes GROUP BY farmacia
ORDER BY 1, 2;


-- =====================================================================================
-- A. Por farmácia e por mês (1º semestre de 2026): total de vendas, vendas com SKU sem
--    cadastro, vendas sem preço/valor, e o % de cada.
--    Colunas de apoio (ajudam a achar a causa):
--      sku_vazio          -> a venda nem tem SKU;
--      casa_outra_grafia  -> o SKU existe no cadastro, mas com a farmácia escrita de outro jeito
--                            ou em outra farmácia (problema no cruzamento, não no cadastro);
--      casa_sem_zeros     -> o SKU só casa tirando zeros à esquerda (tipo número x texto);
--      coletado_depois    -> o SKU só existe em coletas posteriores à venda.
-- =====================================================================================
WITH vendas AS (
  SELECT
    CAST(id_venda AS varchar) AS id_venda,
    farmacia AS farmacia_original,
    regexp_replace(regexp_replace(translate(lower(trim(farmacia)), 'áàâãéêíóôõúç', 'aaaaeeiooouc'), '[^a-z]', ''), '^drogaria', '') AS farmacia_n,
    NULLIF(trim(CAST(sku AS varchar)), '') AS sku_n,
    TRY_CAST(quantidade_vendida AS double) AS quantidade,
    TRY(CAST(substr(CAST(data_venda AS varchar), 1, 10) AS date)) AS dia
  FROM t1_eq1_farmazids.vw_vendas
),
cadastro AS (
  SELECT
    regexp_replace(regexp_replace(translate(lower(trim(farmacia)), 'áàâãéêíóôõúç', 'aaaaeeiooouc'), '[^a-z]', ''), '^drogaria', '') AS farmacia_n,
    trim(CAST(sku AS varchar)) AS sku_n,
    max_by(nome, data_coleta) AS nome,
    -- Mesmo preço da comparação: o Pix; se vazio, o cartão (zero e 9999876 não são preço).
    max_by(COALESCE(
      CASE WHEN TRY_CAST(preco_pix AS double) > 0 AND TRY_CAST(preco_pix AS double) <> 9999876 THEN TRY_CAST(preco_pix AS double) END,
      CASE WHEN TRY_CAST(preco_cartao AS double) > 0 AND TRY_CAST(preco_cartao AS double) <> 9999876 THEN TRY_CAST(preco_cartao AS double) END),
      data_coleta) AS preco,
    min(date(data_coleta)) AS primeira_coleta
  FROM t1_eq1_farmazids.vw_precos_concorrentes
  WHERE sku IS NOT NULL
  GROUP BY 1, 2
),
skus_qualquer_farmacia AS (SELECT DISTINCT sku_n FROM cadastro),
skus_sem_zeros AS (SELECT DISTINCT farmacia_n, regexp_replace(sku_n, '^0+', '') AS sku_sem_zeros FROM cadastro),
classificadas AS (
  SELECT v.*,
    c.nome, c.preco, c.primeira_coleta,
    c.sku_n IS NOT NULL AS cadastrado,
    q.sku_n IS NOT NULL AS sku_existe_em_algum_lugar,
    z.sku_sem_zeros IS NOT NULL AS casa_sem_zeros
  FROM vendas v
  LEFT JOIN cadastro c ON c.farmacia_n = v.farmacia_n AND c.sku_n = v.sku_n
  LEFT JOIN skus_qualquer_farmacia q ON q.sku_n = v.sku_n
  LEFT JOIN skus_sem_zeros z ON z.farmacia_n = v.farmacia_n AND z.sku_sem_zeros = regexp_replace(v.sku_n, '^0+', '')
  WHERE v.dia BETWEEN DATE '2026-01-01' AND DATE '2026-06-30'
)
SELECT
  farmacia_n AS farmacia,
  date_format(dia, '%Y-%m') AS mes,
  count(*) AS vendas,
  count_if(NOT cadastrado) AS sem_cadastro,
  round(100.0 * count_if(NOT cadastrado) / count(*), 1) AS pct_sem_cadastro,
  count_if(preco IS NULL OR quantidade IS NULL) AS sem_valor,
  round(100.0 * count_if(preco IS NULL OR quantidade IS NULL) / count(*), 1) AS pct_sem_valor,
  round(sum(quantidade * preco), 2) AS valor_calculado,
  -- apoio ao diagnóstico
  count_if(sku_n IS NULL) AS sku_vazio,
  count_if(NOT cadastrado AND sku_existe_em_algum_lugar) AS casa_outra_grafia,
  count_if(NOT cadastrado AND casa_sem_zeros) AS casa_sem_zeros,
  count_if(cadastrado AND primeira_coleta > dia) AS coletado_depois
FROM classificadas
GROUP BY 1, 2
ORDER BY 1, 2;


-- =====================================================================================
-- B. Os 20 SKUs sem cadastro que mais aparecem nas vendas (1º semestre de 2026),
--    com quantas vendas, em quais farmácias e se o SKU existe no cadastro de outra farmácia.
-- =====================================================================================
WITH vendas AS (
  SELECT
    farmacia AS farmacia_original,
    regexp_replace(regexp_replace(translate(lower(trim(farmacia)), 'áàâãéêíóôõúç', 'aaaaeeiooouc'), '[^a-z]', ''), '^drogaria', '') AS farmacia_n,
    NULLIF(trim(CAST(sku AS varchar)), '') AS sku_n,
    TRY_CAST(quantidade_vendida AS double) AS quantidade,
    TRY(CAST(substr(CAST(data_venda AS varchar), 1, 10) AS date)) AS dia
  FROM t1_eq1_farmazids.vw_vendas
),
cadastro AS (
  SELECT DISTINCT
    regexp_replace(regexp_replace(translate(lower(trim(farmacia)), 'áàâãéêíóôõúç', 'aaaaeeiooouc'), '[^a-z]', ''), '^drogaria', '') AS farmacia_n,
    trim(CAST(sku AS varchar)) AS sku_n
  FROM t1_eq1_farmazids.vw_precos_concorrentes
  WHERE sku IS NOT NULL
),
sem_cadastro AS (
  SELECT v.*
  FROM vendas v
  LEFT JOIN cadastro c ON c.farmacia_n = v.farmacia_n AND c.sku_n = v.sku_n
  WHERE c.sku_n IS NULL
    AND v.dia BETWEEN DATE '2026-01-01' AND DATE '2026-06-30'
),
outras AS (
  SELECT sku_n, array_join(array_sort(array_agg(farmacia_n)), ', ') AS farmacias
  FROM cadastro GROUP BY sku_n
),
mais_vendidos AS (
  SELECT
    sku_n,
    count(*) AS vendas,
    sum(quantidade) AS unidades,
    array_join(array_sort(array_agg(DISTINCT farmacia_original)), ', ') AS farmacias,
    min(dia) AS primeira_venda,
    max(dia) AS ultima_venda
  FROM sem_cadastro
  GROUP BY sku_n
)
SELECT COALESCE(t.sku_n, '(vazio)') AS sku, t.vendas, t.unidades, t.farmacias, t.primeira_venda, t.ultima_venda,
       o.farmacias AS cadastrado_em_outra_farmacia
FROM mais_vendidos t
LEFT JOIN outras o ON o.sku_n = t.sku_n
ORDER BY t.vendas DESC, sku
LIMIT 20;


-- =====================================================================================
-- C. O caso do cliente: FarmaPonte, janeiro de 2026, dipirona.
--    Lista TODAS as vendas da FarmaPonte em janeiro, com o que o cadastro diz de cada SKU.
--    Venda com SKU sem cadastro não tem nome nem preço: por isso não dá para saber, só pelo
--    cadastro, se ela é de dipirona. A coluna eh_dipirona_no_cadastro marca as que o cadastro
--    confirma; se a vw_vendas tiver o nome do produto (ver o PASSO 0), acrescente-o em
--    "vendas" e compare.
--    Para ver exatamente como o chatbot montou a resposta, abra a execução da pergunta do
--    cliente no console do Step Functions: o SQL está na saída do passo GerarSQL.
-- =====================================================================================
WITH vendas AS (
  SELECT
    CAST(id_venda AS varchar) AS id_venda,
    farmacia AS farmacia_original,
    regexp_replace(regexp_replace(translate(lower(trim(farmacia)), 'áàâãéêíóôõúç', 'aaaaeeiooouc'), '[^a-z]', ''), '^drogaria', '') AS farmacia_n,
    NULLIF(trim(CAST(sku AS varchar)), '') AS sku_n,
    TRY_CAST(quantidade_vendida AS double) AS quantidade,
    TRY(CAST(substr(CAST(data_venda AS varchar), 1, 10) AS date)) AS dia
  FROM t1_eq1_farmazids.vw_vendas
),
cadastro AS (
  SELECT
    regexp_replace(regexp_replace(translate(lower(trim(farmacia)), 'áàâãéêíóôõúç', 'aaaaeeiooouc'), '[^a-z]', ''), '^drogaria', '') AS farmacia_n,
    trim(CAST(sku AS varchar)) AS sku_n,
    max_by(nome, data_coleta) AS nome,
    max_by(COALESCE(
      CASE WHEN TRY_CAST(preco_pix AS double) > 0 AND TRY_CAST(preco_pix AS double) <> 9999876 THEN TRY_CAST(preco_pix AS double) END,
      CASE WHEN TRY_CAST(preco_cartao AS double) > 0 AND TRY_CAST(preco_cartao AS double) <> 9999876 THEN TRY_CAST(preco_cartao AS double) END),
      data_coleta) AS preco,
    min(date(data_coleta)) AS primeira_coleta
  FROM t1_eq1_farmazids.vw_precos_concorrentes
  WHERE sku IS NOT NULL
  GROUP BY 1, 2
)
SELECT
  v.id_venda, v.dia, v.farmacia_original, v.sku_n AS sku, v.quantidade,
  c.nome AS nome_no_cadastro,
  c.preco AS preco_no_cadastro,
  round(v.quantidade * c.preco, 2) AS valor_calculado,
  CASE WHEN v.sku_n IS NULL THEN 'sem SKU'
       WHEN c.sku_n IS NULL THEN 'SKU sem cadastro'
       WHEN c.preco IS NULL THEN 'cadastrado, sem preço'
       ELSE 'ok' END AS situacao,
  lower(c.nome) LIKE '%dipirona%' AS eh_dipirona_no_cadastro,
  c.primeira_coleta
FROM vendas v
LEFT JOIN cadastro c ON c.farmacia_n = v.farmacia_n AND c.sku_n = v.sku_n
WHERE v.farmacia_n = 'farmaponte'
  AND v.dia BETWEEN DATE '2026-01-01' AND DATE '2026-01-31'
ORDER BY eh_dipirona_no_cadastro DESC NULLS LAST, situacao, v.dia
LIMIT 200;
