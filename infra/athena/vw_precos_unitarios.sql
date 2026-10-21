-- Preço por unidade (comprimido, cápsula, ml, g...) dos concorrentes.
-- Gerado por infra/athena/regras_extracao.py: altere as regras lá e gere de novo, não edite à mão.
-- Não altera a view de origem (vw_precos_concorrentes): só lê dela e acrescenta as colunas
--   quantidade, unidade, concentracao, forma e o preço por unidade.
-- Para comparar farmácias: preco_comparacao (o Pix; se vazio, o cartão), preco_comparacao_origem
--   ('pix' ou 'cartao') e preco_comparacao_por_unidade. A São João não informa Pix.
-- Quando a quantidade não aparece no nome, ela fica nula (o preço por unidade também): nunca é chutada.

CREATE OR REPLACE VIEW t1_eq1_farmazids.vw_precos_unitarios AS
WITH base AS (
  SELECT farmacia, sku, ean, nome, marca, categorias, preco_sem_desconto, preco_cartao, preco_pix, em_estoque, desconto_valor, desconto_percentual, url, data_coleta, dt,
    ' ' || regexp_replace(replace(regexp_replace(translate(lower(nome), 'áàâãäéèêëíìîïóòôõöúùûüç', 'aaaaaeeeeiiiiooooouuuuc'),
        '(?<![\d.,])([1-9]\d{0,2})(?:\.|\s(?=\d{3}\s*ui))(\d{3})(?![\d.,])', '$1$2'), ',', '.'),
        '\bc/(\d)', 'c/ $1') || ' ' AS t
  FROM t1_eq1_farmazids.vw_precos_concorrentes
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
  CASE WHEN qtd > 0 THEN round(COALESCE(CASE WHEN TRY_CAST(preco_pix AS double) > 0 AND TRY_CAST(preco_pix AS double) <> 9999876 THEN TRY_CAST(preco_pix AS double) END, CASE WHEN TRY_CAST(preco_cartao AS double) > 0 AND TRY_CAST(preco_cartao AS double) <> 9999876 THEN TRY_CAST(preco_cartao AS double) END) / qtd, 4) END AS preco_comparacao_por_unidade
FROM passo3
