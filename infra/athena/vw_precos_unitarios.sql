-- Preço por unidade (comprimido, cápsula, ml, g...) dos concorrentes.
-- Gerado por infra/athena/regras_extracao.py: altere as regras lá e gere de novo, não edite à mão.
-- Não altera a view de origem (vw_precos_concorrentes): só lê dela e acrescenta as colunas
--   quantidade, unidade, concentracao, forma e o preço por unidade.
-- Para comparar farmácias: preco_comparacao (o Pix; se vazio, o cartão), preco_comparacao_origem
--   ('pix' ou 'cartao') e preco_comparacao_por_unidade. A São João não informa Pix.
-- Marca: escrita padronizada (acentos e maiúsculas certos); vazia, vem do mesmo EAN nas outras farmácias
--   (coleta mais recente; a mais frequente; empate, ou nome citando outra marca, fica vazio).
--   marca_origem: 'coleta' ou 'ean'.
-- Quando a quantidade não aparece no nome, ela fica nula (o preço por unidade também): nunca é chutada.

CREATE OR REPLACE VIEW t1_eq1_farmazids.vw_precos_unitarios AS
WITH fonte AS (
  SELECT c.*,
    CASE WHEN lower(trim(CAST(c.marca AS varchar))) IN ('', 'none', 'null', 'nan') THEN NULL
         ELSE trim(CAST(c.marca AS varchar)) END AS marca_lida,
    regexp_replace(trim(CAST(c.ean AS varchar)), '^0+', '') AS ean_n,
    ' ' || trim(regexp_replace(translate(lower(c.nome), 'áàâãäéèêëíìîïóòôõöúùûüç', 'aaaaaeeeeiiiiooooouuuuc'), '[^a-z0-9]+', ' ')) || ' ' AS nome_n
  FROM t1_eq1_farmazids.vw_precos_concorrentes c
),
grafias AS (
  -- a melhor grafia de cada marca: nada de nome longo todo em maiúsculas; depois a com mais acentos
  -- ("Aché", "Farmoquímica") e mais maiúsculas ("EMS", "GSK", "FiberMais"); depois a mais usada
  SELECT chave, element_at(array_agg(marca_lida ORDER BY
      CASE WHEN marca_lida = upper(marca_lida) AND length(regexp_replace(marca_lida, '[^\p{L}]', '')) > 4 THEN 1 ELSE 0 END,
      length(regexp_replace(marca_lida, '[^\p{L}]|[A-Za-z]', '')) DESC,
      length(regexp_replace(marca_lida, '[^\p{Lu}]', '')) DESC,
      n DESC, marca_lida), 1) AS marca_padrao
  FROM (SELECT regexp_replace(translate(lower(marca_lida), 'áàâãäéèêëíìîïóòôõöúùûüç', 'aaaaaeeeeiiiiooooouuuuc'), '[^a-z0-9]', '') AS chave, marca_lida, count(*) AS n
        FROM fonte WHERE marca_lida IS NOT NULL GROUP BY 1, 2)
  GROUP BY chave
),
marcas_conhecidas AS (
  -- laboratórios do cadastro, em palavras: marcas em produtos de pelo menos 5 nomes diferentes (pela
  -- primeira palavra do nome). Linha de produto ("Bravan", "Neosaldina") fica de fora: tem 1 ou 2.
  SELECT DISTINCT palavras FROM (
    SELECT trim(regexp_replace(translate(lower(g.marca_padrao), 'áàâãäéèêëíìîïóòôõöúùûüç', 'aaaaaeeeeiiiiooooouuuuc'), '[^a-z0-9]+', ' ')) AS palavras
    FROM grafias g
    JOIN (SELECT regexp_replace(translate(lower(marca_lida), 'áàâãäéèêëíìîïóòôõöúùûüç', 'aaaaaeeeeiiiiooooouuuuc'), '[^a-z0-9]', '') AS chave, count(DISTINCT split_part(trim(nome_n), ' ', 1)) AS nomes
          FROM fonte WHERE marca_lida IS NOT NULL GROUP BY 1) v ON v.chave = g.chave
    WHERE v.nomes >= 5)
  WHERE palavras <> ''
),
ultima_coleta AS (
  SELECT farmacia, max(date(data_coleta)) AS dia FROM fonte GROUP BY farmacia
),
marcas_ean AS (
  -- a marca de cada EAN em cada farmácia, na coleta mais recente dela
  SELECT DISTINCT f.farmacia, f.ean_n, g.marca_padrao
  FROM fonte f
  JOIN ultima_coleta u ON u.farmacia = f.farmacia AND date(f.data_coleta) = u.dia
  JOIN grafias g ON g.chave = regexp_replace(translate(lower(f.marca_lida), 'áàâãäéèêëíìîïóòôõöúùûüç', 'aaaaaeeeeiiiiooooouuuuc'), '[^a-z0-9]', '')
  WHERE f.marca_lida IS NOT NULL AND f.ean_n <> ''
),
votos AS (
  -- para cada farmácia, quantas OUTRAS farmácias dão cada marca ao EAN
  SELECT a.farmacia AS alvo, m.ean_n, m.marca_padrao, count(*) AS n
  FROM (SELECT DISTINCT farmacia FROM fonte) a
  JOIN marcas_ean m ON m.farmacia <> a.farmacia
  GROUP BY 1, 2, 3
),
escolha AS (
  -- a mais frequente; empate fica sem marca
  SELECT alvo, ean_n, marca_padrao FROM (
    SELECT votos.*, rank() OVER (PARTITION BY alvo, ean_n ORDER BY n DESC) AS posicao,
      count(*) OVER (PARTITION BY alvo, ean_n, n) AS empatadas
    FROM votos)
  WHERE posicao = 1 AND empatadas = 1
),
candidatas AS (
  -- marca votada para cada nome sem marca
  SELECT DISTINCT f.nome_n, e.marca_padrao AS votada
  FROM fonte f JOIN escolha e ON e.alvo = f.farmacia AND e.ean_n = f.ean_n
  WHERE f.marca_lida IS NULL
),
conflitos AS (
  -- o nome cita outro laboratório (palavra inteira, sem acento): "... Genérico Biosintética" não vira
  -- "Medley". Se uma marca contém a outra ("EMS Genérico" e "EMS"), é a mesma empresa: não conta.
  SELECT DISTINCT c.nome_n, c.votada
  FROM (SELECT nome_n, votada, trim(regexp_replace(translate(lower(votada), 'áàâãäéèêëíìîïóòôõöúùûüç', 'aaaaaeeeeiiiiooooouuuuc'), '[^a-z0-9]+', ' ')) AS votada_palavras FROM candidatas) c
  JOIN marcas_conhecidas k
    ON strpos(c.nome_n, ' ' || k.palavras || ' ') > 0
   AND strpos(' ' || c.votada_palavras || ' ', ' ' || k.palavras || ' ') = 0
   AND strpos(' ' || k.palavras || ' ', ' ' || c.votada_palavras || ' ') = 0
),
com_marca AS (
  SELECT f.*,
    COALESCE(g.marca_padrao, f.marca_lida, CASE WHEN x.votada IS NULL THEN e.marca_padrao END) AS marca_final,
    CASE WHEN f.marca_lida IS NOT NULL THEN 'coleta'
         WHEN e.marca_padrao IS NOT NULL AND x.votada IS NULL THEN 'ean' END AS marca_origem
  FROM fonte f
  LEFT JOIN grafias g ON g.chave = regexp_replace(translate(lower(f.marca_lida), 'áàâãäéèêëíìîïóòôõöúùûüç', 'aaaaaeeeeiiiiooooouuuuc'), '[^a-z0-9]', '')
  LEFT JOIN escolha e ON f.marca_lida IS NULL AND e.alvo = f.farmacia AND e.ean_n = f.ean_n
  LEFT JOIN conflitos x ON x.nome_n = f.nome_n AND x.votada = e.marca_padrao
),
base AS (
  SELECT farmacia, sku, ean, nome, marca_final AS marca, categorias, preco_sem_desconto, preco_cartao, preco_pix, em_estoque, desconto_valor, desconto_percentual, url, data_coleta, dt, marca_origem,
    ' ' || regexp_replace(replace(regexp_replace(translate(lower(nome), 'áàâãäéèêëíìîïóòôõöúùûüç', 'aaaaaeeeeiiiiooooouuuuc'),
        '(?<![\d.,])([1-9]\d{0,2})(?:\.|\s(?=\d{3}\s*ui))(\d{3})(?![\d.,])', '$1$2'), ',', '.'),
        '\bc/(\d)', 'c/ $1') || ' ' AS t
  FROM com_marca
),
passo1 AS (
  SELECT base.*,
    -- 1. quantidade contável ("c/20 cpr" e "com 30 comprimidos" primeiro; senão, a última do nome)
    COALESCE(regexp_extract(t, '(?:\bc/|\bcom)\s*(\d+(?:\.\d+)?)\s*(comprimid\w*|conprimid\w*|comp\.?|cprs?\.?|cpd|cp\.?|capsulas?|caps?\.?|cps|drageas?|drg|saches?|sachets?|envelopes?|env|pastilhas?|gomas?|ampolas?|amp|flaconetes?|supositorios?|ovulos?|adesivos?|seringas?|doses|acionamentos|jatos|unidades?|unid\.?|und\.?|un\.?)(?![a-z])', 1),
             element_at(regexp_extract_all(t, '(?<![\d.])(\d+(?:\.\d+)?)\s*(comprimid\w*|conprimid\w*|comp\.?|cprs?\.?|cpd|cp\.?|capsulas?|caps?\.?|cps|drageas?|drg|saches?|sachets?|envelopes?|env|pastilhas?|gomas?|ampolas?|amp|flaconetes?|supositorios?|ovulos?|adesivos?|seringas?|doses|acionamentos|jatos|unidades?|unid\.?|und\.?|un\.?)(?![a-z])', 1), -1)) AS n_cont,
    CASE WHEN regexp_like(t, '(?:\bc/|\bcom)\s*(\d+(?:\.\d+)?)\s*(comprimid\w*|conprimid\w*|comp\.?|cprs?\.?|cpd|cp\.?|capsulas?|caps?\.?|cps|drageas?|drg|saches?|sachets?|envelopes?|env|pastilhas?|gomas?|ampolas?|amp|flaconetes?|supositorios?|ovulos?|adesivos?|seringas?|doses|acionamentos|jatos|unidades?|unid\.?|und\.?|un\.?)(?![a-z])') THEN regexp_extract(t, '(?:\bc/|\bcom)\s*(\d+(?:\.\d+)?)\s*(comprimid\w*|conprimid\w*|comp\.?|cprs?\.?|cpd|cp\.?|capsulas?|caps?\.?|cps|drageas?|drg|saches?|sachets?|envelopes?|env|pastilhas?|gomas?|ampolas?|amp|flaconetes?|supositorios?|ovulos?|adesivos?|seringas?|doses|acionamentos|jatos|unidades?|unid\.?|und\.?|un\.?)(?![a-z])', 2)
         ELSE element_at(regexp_extract_all(t, '(?<![\d.])(\d+(?:\.\d+)?)\s*(comprimid\w*|conprimid\w*|comp\.?|cprs?\.?|cpd|cp\.?|capsulas?|caps?\.?|cps|drageas?|drg|saches?|sachets?|envelopes?|env|pastilhas?|gomas?|ampolas?|amp|flaconetes?|supositorios?|ovulos?|adesivos?|seringas?|doses|acionamentos|jatos|unidades?|unid\.?|und\.?|un\.?)(?![a-z])', 2), -1) END AS u_cont,
    -- forma (a primeira que aparecer na lista)
    CASE
          WHEN regexp_like(t, 'comprimid|conprimid|\bcomp\b|\bcpr|\bcp\b|\bcpd\b') THEN 'comprimido'
          WHEN regexp_like(t, 'capsula|\bcaps?\b|\bcps\b') THEN 'capsula'
          WHEN regexp_like(t, 'dragea|\bdrg\b') THEN 'dragea'
          WHEN regexp_like(t, 'sache|envelope') THEN 'sache'
          WHEN regexp_like(t, 'pastilha|\bgomas?\b') THEN 'pastilha'
          WHEN regexp_like(t, 'gotas|\bgts\b') THEN 'gotas'
          WHEN regexp_like(t, 'xarope') THEN 'xarope'
          WHEN regexp_like(t, 'injetavel|ampola|\bamp\b|seringa|caneta') THEN 'injetavel'
          WHEN regexp_like(t, 'suspensao') THEN 'suspensao'
          WHEN regexp_like(t, 'solucao|\bsol\b') THEN 'solucao'
          WHEN regexp_like(t, 'spray|aerossol|inalat') THEN 'spray'
          WHEN regexp_like(t, 'colirio') THEN 'colirio'
          WHEN regexp_like(t, 'pomada') THEN 'pomada'
          WHEN regexp_like(t, 'creme') THEN 'creme'
          WHEN regexp_like(t, '\bgel\b') THEN 'gel'
          WHEN regexp_like(t, 'locao') THEN 'locao'
          WHEN regexp_like(t, 'supositorio') THEN 'supositorio'
          WHEN regexp_like(t, 'ovulo') THEN 'ovulo'
          WHEN regexp_like(t, 'adesivo') THEN 'adesivo'
          WHEN regexp_like(t, '\bpo\b') THEN 'po'
          WHEN regexp_like(t, 'shampoo') THEN 'shampoo'
        END AS forma_base
  FROM base
),
passo2 AS (
  SELECT passo1.*,
    CAST(n_cont AS double) AS qtd_cont,
    CASE
          WHEN regexp_like(u_cont, '^(comprimid\w*|conprimid\w*|comp\.?|cprs?\.?|cpd|cp\.?)$') THEN 'comprimido'
          WHEN regexp_like(u_cont, '^(capsulas?|caps?\.?|cps)$') THEN 'capsula'
          WHEN regexp_like(u_cont, '^(drageas?|drg)$') THEN 'dragea'
          WHEN regexp_like(u_cont, '^(saches?|sachets?|envelopes?|env)$') THEN 'sache'
          WHEN regexp_like(u_cont, '^(pastilhas?|gomas?)$') THEN 'pastilha'
          WHEN regexp_like(u_cont, '^(ampolas?|amp)$') THEN 'ampola'
          WHEN regexp_like(u_cont, '^flaconetes?$') THEN 'flaconete'
          WHEN regexp_like(u_cont, '^supositorios?$') THEN 'supositorio'
          WHEN regexp_like(u_cont, '^ovulos?$') THEN 'ovulo'
          WHEN regexp_like(u_cont, '^adesivos?$') THEN 'adesivo'
          WHEN regexp_like(u_cont, '^seringas?$') THEN 'seringa'
          WHEN regexp_like(u_cont, '^(doses|acionamentos|jatos)$') THEN 'dose'
          WHEN regexp_like(u_cont, '^(unidades?|unid\.?|und\.?|un\.?)$') THEN 'unidade'
        END AS unid_cont,
    CASE
      WHEN forma_base IN ('comprimido', 'capsula', 'dragea') AND regexp_like(t, 'efervescente') THEN forma_base || ' efervescente'
      WHEN forma_base IN ('comprimido', 'capsula', 'dragea') AND regexp_like(t, '(libera\w*|acao)\s+(prolongada|controlada|modificada|estendida|lenta|retardada)|\b(xr|xl|cr|sr|la|lp|retard)\b') THEN forma_base || ' liberacao prolongada'
      ELSE forma_base
    END AS forma,
    -- volume/massa (só usado quando não há quantidade contável); "g" só em forma semissólida ou sem forma
    element_at(filter(regexp_extract_all(t, '(?<![\d./])(\d+(?:\.\d+)?)\s*(ml|l|gr?|kg)(?![a-z/])(?!\s*/)'),
                      v -> NOT regexp_like(v, '\d\s*gr?$') OR forma_base IN ('pomada', 'creme', 'gel', 'locao', 'po', 'shampoo') OR forma_base IS NULL),
               -1) AS vol
  FROM passo1
),
passo3 AS (
  SELECT passo2.*,
    CASE WHEN qtd_cont IS NOT NULL THEN qtd_cont
         WHEN regexp_extract(vol, '(ml|l|gr?|kg)$', 1) IN ('l', 'kg') THEN CAST(regexp_extract(vol, '(\d+(?:\.\d+)?)', 1) AS double) * 1000
         ELSE CAST(regexp_extract(vol, '(\d+(?:\.\d+)?)', 1) AS double) END AS qtd,
    CASE WHEN qtd_cont IS NOT NULL THEN unid_cont
         WHEN regexp_extract(vol, '(ml|l|gr?|kg)$', 1) = 'l' THEN 'ml'
         WHEN regexp_extract(vol, '(ml|l|gr?|kg)$', 1) IN ('kg', 'gr') THEN 'g'
         ELSE regexp_extract(vol, '(ml|l|gr?|kg)$', 1) END AS unid,
    -- concentração: todas as do nome, juntas com "+"; "1g" vira "1000mg"; "g" solto só conta em produto contável
    NULLIF(array_join(transform(
      filter(
        transform(regexp_extract_all(t, '(?<![\d.])(\d+(?:\.\d+)?)\s*(mg|mcg|ug|ui|%|g)(?:\s*/\s*((?:\d+(?:\.\d+)?)?\s*(?:ml|g|dose|gota)))?(?![a-z])(?!\s+(?:de|cada)\s)'),
                  c -> CAST(ROW(regexp_extract(c, '^(\d+(?:\.\d+)?)\s*(mg|mcg|ug|ui|%|g)(?:\s*/\s*((?:\d+(?:\.\d+)?)?\s*(?:ml|g|dose|gota)))?', 1),
                                regexp_extract(c, '^(\d+(?:\.\d+)?)\s*(mg|mcg|ug|ui|%|g)(?:\s*/\s*((?:\d+(?:\.\d+)?)?\s*(?:ml|g|dose|gota)))?', 2),
                                regexp_extract(c, '^(\d+(?:\.\d+)?)\s*(mg|mcg|ug|ui|%|g)(?:\s*/\s*((?:\d+(?:\.\d+)?)?\s*(?:ml|g|dose|gota)))?', 3))
                            AS ROW(n varchar, u varchar, por varchar))),
        p -> NOT (p.u = 'g' AND p.por IS NULL AND (unid_cont IS NULL OR unid_cont IN ('dose', 'unidade')))),
      p -> regexp_replace(CAST(CAST(CASE WHEN p.u = 'g' AND p.por IS NULL THEN CAST(p.n AS double) * 1000 ELSE CAST(p.n AS double) END AS decimal(18, 4)) AS varchar), '\.?0+$', '')
           || CASE WHEN p.u = 'g' AND p.por IS NULL THEN 'mg' WHEN p.u = 'ug' THEN 'mcg' ELSE p.u END
           || COALESCE('/' || replace(p.por, ' ', ''), '')), '+'), '') AS concentracao
  FROM passo2
)
SELECT farmacia, sku, ean, nome, marca, categorias, preco_sem_desconto, preco_cartao, preco_pix, em_estoque, desconto_valor, desconto_percentual, url, data_coleta, dt,
  CASE WHEN qtd > 0 THEN qtd END AS quantidade,
  CASE WHEN qtd > 0 THEN unid END AS unidade,
  concentracao,
  forma,
  CASE WHEN qtd > 0 AND TRY_CAST(preco_pix AS double) > 0 AND TRY_CAST(preco_pix AS double) <> 9999876
       THEN round(TRY_CAST(preco_pix AS double) / qtd, 4) END AS preco_pix_por_unidade,
  CASE WHEN qtd > 0 AND TRY_CAST(preco_cartao AS double) > 0 AND TRY_CAST(preco_cartao AS double) <> 9999876
       THEN round(TRY_CAST(preco_cartao AS double) / qtd, 4) END AS preco_cartao_por_unidade,
  COALESCE(CASE WHEN TRY_CAST(preco_pix AS double) > 0 AND TRY_CAST(preco_pix AS double) <> 9999876 THEN TRY_CAST(preco_pix AS double) END, CASE WHEN TRY_CAST(preco_cartao AS double) > 0 AND TRY_CAST(preco_cartao AS double) <> 9999876 THEN TRY_CAST(preco_cartao AS double) END) AS preco_comparacao,
  CASE WHEN CASE WHEN TRY_CAST(preco_pix AS double) > 0 AND TRY_CAST(preco_pix AS double) <> 9999876 THEN TRY_CAST(preco_pix AS double) END IS NOT NULL THEN 'pix' WHEN CASE WHEN TRY_CAST(preco_cartao AS double) > 0 AND TRY_CAST(preco_cartao AS double) <> 9999876 THEN TRY_CAST(preco_cartao AS double) END IS NOT NULL THEN 'cartao' END AS preco_comparacao_origem,
  CASE WHEN qtd > 0 THEN round(COALESCE(CASE WHEN TRY_CAST(preco_pix AS double) > 0 AND TRY_CAST(preco_pix AS double) <> 9999876 THEN TRY_CAST(preco_pix AS double) END, CASE WHEN TRY_CAST(preco_cartao AS double) > 0 AND TRY_CAST(preco_cartao AS double) <> 9999876 THEN TRY_CAST(preco_cartao AS double) END) / qtd, 4) END AS preco_comparacao_por_unidade,
  marca_origem
FROM passo3
