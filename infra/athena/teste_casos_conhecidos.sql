-- Teste do SQL das views com nomes conhecidos (não cria nada no Athena).
-- Gerado por infra/athena/regras_extracao.py. Esperado: a coluna confere = true em todas as linhas.
-- Se alguma vier false, o SQL do Athena não está fazendo o mesmo que o teste em Python.

WITH base AS (
  SELECT nome, esperado_quantidade, esperado_unidade, esperado_concentracao, esperado_forma,
    ' ' || regexp_replace(replace(regexp_replace(translate(lower(nome), 'áàâãäéèêëíìîïóòôõöúùûüç', 'aaaaaeeeeiiiiooooouuuuc'),
        '(?<![\d.,])([1-9]\d{0,2})(?:\.|\s(?=\d{3}\s*ui))(\d{3})(?![\d.,])', '$1$2'), ',', '.'),
        '\bc/(\d)', 'c/ $1') || ' ' AS t
  FROM (VALUES
  ('Dipirona 1g 10 Comprimidos', CAST(10.0 AS double), 'comprimido', '1000mg', 'comprimido'),
  ('Dipirona 1.000mg 20 comprimidos', CAST(20.0 AS double), 'comprimido', '1000mg', 'comprimido'),
  ('PONDERA 15 MG C/20 CPR', CAST(20.0 AS double), 'comprimido', '15mg', 'comprimido'),
  ('Dipirona Gotas 500mg/ml 20ml', CAST(20.0 AS double), 'ml', '500mg/ml', 'gotas'),
  ('AMPLICTIL 4% GTS C/20 ML', CAST(20.0 AS double), 'ml', '4%', 'gotas'),
  ('Zoladex 3,6mg 1 seringa', CAST(1.0 AS double), 'seringa', '3.6mg', 'injetavel'),
  ('Demedrox 150mg/ml Suspensão Injetável 1 seringa + 1agulha', CAST(1.0 AS double), 'seringa', '150mg/ml', 'injetavel'),
  ('Tandene Diclofenaco Sódico 50mg + Carisoprodol 125mg + Paracetamol 300mg + Cafeína 30mg 30 comprimid', CAST(30.0 AS double), 'comprimido', '50mg+125mg+300mg+30mg', 'comprimido'),
  ('Soapex Sabonete Liquido 1 120 Ml', CAST(120.0 AS double), 'ml', CAST(NULL AS varchar), CAST(NULL AS varchar)),
  ('Hemax Eritron 10 000UI Inj 1Fr/Amp Biosintetica', CAST(NULL AS double), CAST(NULL AS varchar), '10000ui', 'injetavel'),
  ('Nistatina Creme Vaginal Neo Química 25.000UI/g, caixa com 1 bisnaga com 60g de creme', CAST(60.0 AS double), 'g', '25000ui/g', 'creme'),
  ('Drenison N 0,125mg/g + 0,35mg/g Creme 30g', CAST(30.0 AS double), 'g', '0.125mg/g+0.35mg/g', 'creme'),
  ('Biconcor 5,0mg Merck 30 comprimidos', CAST(30.0 AS double), 'comprimido', '5mg', 'comprimido'),
  ('Lescol XL 80mg Novartis 30 Comprimidos', CAST(30.0 AS double), 'comprimido', '80mg', 'comprimido liberacao prolongada'),
  ('Zomig Od 2,5mg 2 Comprimidos Orodispersíveis', CAST(2.0 AS double), 'comprimido', '2.5mg', 'comprimido'),
  ('Cebion 1g 10 Comprimidos Efervescentes', CAST(10.0 AS double), 'comprimido', '1000mg', 'comprimido efervescente'),
  ('Glifage XR 500mg 30 comprimidos de liberação prolongada', CAST(30.0 AS double), 'comprimido', '500mg', 'comprimido liberacao prolongada'),
  ('Aeroflux 2mg/5ml + 100mg/5ml GSK 120ml Solução', CAST(120.0 AS double), 'ml', '2mg/5ml+100mg/5ml', 'solucao'),
  ('Glyquin XM 40mg/g + 100mg/g, caixa com 28 sachês com 1g de creme de uso dermatológico', CAST(28.0 AS double), 'sache', '40mg/g+100mg/g', 'sache'),
  ('Suplemento Alimentar Sarcopore Sabor Tangerina 30 Sachês com 16,10g Cada', CAST(30.0 AS double), 'sache', CAST(NULL AS varchar), 'sache'),
  ('BALSAMO BENGUE AERO 120GR', CAST(120.0 AS double), 'g', CAST(NULL AS varchar), CAST(NULL AS varchar)),
  ('Ventus Spray 50mcg 60 Acionamentos 9ml', CAST(60.0 AS double), 'dose', '50mcg', 'spray'),
  ('Triancil 20mg/mL Frasco/ampola 5mL Injetável Apsen 1 Frasco', CAST(5.0 AS double), 'ml', '20mg/ml', 'injetavel'),
  ('Denyl 20mg comprimidos', CAST(NULL AS double), CAST(NULL AS varchar), '20mg', 'comprimido')
) AS casos(nome, esperado_quantidade, esperado_unidade, esperado_concentracao, esperado_forma)
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
, resultado AS (
SELECT nome, esperado_quantidade, esperado_unidade, esperado_concentracao, esperado_forma,
  CASE WHEN qtd > 0 THEN qtd END AS quantidade,
  CASE WHEN qtd > 0 THEN unid END AS unidade,
  concentracao,
  forma
FROM passo3
)
SELECT nome,
  quantidade, esperado_quantidade, unidade, esperado_unidade,
  concentracao, esperado_concentracao, forma, esperado_forma,
  quantidade IS NOT DISTINCT FROM esperado_quantidade
    AND unidade IS NOT DISTINCT FROM esperado_unidade
    AND concentracao IS NOT DISTINCT FROM esperado_concentracao
    AND forma IS NOT DISTINCT FROM esperado_forma AS confere
FROM resultado
ORDER BY confere, nome
