-- Comparacao base_projecao_emissao_guilherme vs base_projecao_emissao_igor
-- pra Mai/26 (mes completo, antes do pipeline do Igor quebrar)
-- Esperado: contagens batem +/- 1% por (FLAG_TC, DT_CONV, super_grupo)

WITH g AS (
  SELECT FLAG_TC, DT_CONV,
    CASE
      WHEN COALESCE(PLACEMENT,'') = 'EA'                         THEN 'EA'
      WHEN FLAG_SELLERS IN ('SELLER','MIXTO')                    THEN 'Sellers'
      WHEN grupo_especial LIKE '%Mar Aberto%'                    THEN 'Mar Aberto'
      WHEN grupo_especial = 'TEST REACH-TEST NO ECOSISTEMATICOS' THEN 'Only Nav'
      WHEN grupo_especial LIKE '%CANCELADAS%'                    THEN 'Cuentas Canceladas'
      ELSE 'BAU'
    END AS super_grupo,
    SUM(QTDE) AS total_guilherme
  FROM `meli-bi-data.SBOX_CREDITSTC.base_projecao_emissao_guilherme`
  WHERE FLAG_CONVERSAO = '1. Convertido'
    AND DT_CONV >= '2026-05-01' AND DT_CONV < '2026-06-01'
  GROUP BY 1, 2, 3
),
i AS (
  SELECT FLAG_TC, DT_CONV,
    CASE
      WHEN COALESCE(PLACEMENT,'') = 'EA'                         THEN 'EA'
      WHEN FLAG_SELLERS IN ('SELLER','MIXTO')                    THEN 'Sellers'
      WHEN grupo_especial LIKE '%Mar Aberto%'                    THEN 'Mar Aberto'
      WHEN grupo_especial = 'TEST REACH-TEST NO ECOSISTEMATICOS' THEN 'Only Nav'
      WHEN grupo_especial LIKE '%CANCELADAS%'                    THEN 'Cuentas Canceladas'
      ELSE 'BAU'
    END AS super_grupo,
    SUM(QTDE) AS total_igor
  FROM `meli-bi-data.SBOX_CREDITSTC.base_projecao_emissao_igor`
  WHERE FLAG_CONVERSAO = '1. Convertido'
    AND DT_CONV >= '2026-05-01' AND DT_CONV < '2026-06-01'
  GROUP BY 1, 2, 3
)
SELECT
  COALESCE(g.FLAG_TC, i.FLAG_TC) AS FLAG_TC,
  COALESCE(g.super_grupo, i.super_grupo) AS super_grupo,
  SUM(COALESCE(g.total_guilherme, 0)) AS total_guilherme,
  SUM(COALESCE(i.total_igor, 0)) AS total_igor,
  SUM(COALESCE(g.total_guilherme, 0)) - SUM(COALESCE(i.total_igor, 0)) AS diff,
  SAFE_DIVIDE(SUM(COALESCE(g.total_guilherme, 0)) - SUM(COALESCE(i.total_igor, 0)), SUM(COALESCE(i.total_igor, 0))) AS pct_diff
FROM g FULL OUTER JOIN i USING(FLAG_TC, DT_CONV, super_grupo)
GROUP BY 1, 2
ORDER BY 1, 2
