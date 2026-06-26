-- ============================================================
-- DASHBOARD PROJECAO (por super_grupo) -- versao base_projecao_Gui
-- Fonte: proj_base_slim_gui (tabela enxuta; ver build_proj_base_slim.sql)
-- Retorna: FLAG_TC, super_grupo, dia, total, tipo ('actual' | 'proj')
--
-- DIFERENCAS vs dashboard_sg.sql (base guilherme):
--   - FROM proj_base_slim_gui (recorte 4m, dims pre-calculadas)
--   - Sellers via FLAG_NISE='0. SELLER' (ja embutido em super_grupo_base/nise_seller)
--   - EA via flag_ea (canal 'EA - MP'); base nova nao tem PLACEMENT
--   - APP_INST REMOVIDO da curva (base nova nao tem a coluna)
--   - CTEs mortos removidos (qtde_enc_mes_atual/prev, prev_daily_template)
--   - Spike alignment continua em WHOWNER.BT_CCARD_PROPOSAL (inalterado)
-- ============================================================
WITH
datas AS (
  SELECT
    CURRENT_DATE()                    AS data_hoje,
    DATE_TRUNC(CURRENT_DATE(), MONTH) AS inicio_mes,
    LAST_DAY(CURRENT_DATE())          AS fim_mes
),

hist AS (
  SELECT
    FLAG_TC, FLAG_REENCENDIDO, nise_seller,
    super_grupo_base AS super_grupo,
    flag_ea, FLAG_CONVERSAO, DIAS_CONV, QTDE
  FROM `meli-bi-data.SBOX_CREDITSTC.proj_base_slim_gui`
  WHERE DT_ENCENDIDO >= DATE_TRUNC(DATE_SUB(DATE_TRUNC(CURRENT_DATE(), MONTH), INTERVAL 3 MONTH), MONTH)
    AND DT_ENCENDIDO <  DATE_TRUNC(CURRENT_DATE(), MONTH)
    AND EXTRACT(MONTH FROM DT_ENCENDIDO) NOT IN (11, 12)
),
total_hist AS (
  SELECT FLAG_TC, FLAG_REENCENDIDO, nise_seller, super_grupo, SUM(QTDE) AS total_enc
  FROM hist GROUP BY ALL
),
conv_por_dia AS (
  SELECT FLAG_TC, FLAG_REENCENDIDO, nise_seller, super_grupo,
         DIAS_CONV AS dia,
         SUM(CASE WHEN FLAG_CONVERSAO = '1. Convertido'
                   AND NOT flag_ea THEN QTDE ELSE 0 END) AS conv_dia
  FROM hist
  WHERE DIAS_CONV IS NOT NULL AND DIAS_CONV <= 59
  GROUP BY ALL
),
grade AS (
  SELECT t.FLAG_TC, t.FLAG_REENCENDIDO, t.nise_seller, t.super_grupo, d AS dia
  FROM total_hist t CROSS JOIN UNNEST(GENERATE_ARRAY(0, 59)) AS d
),
curva AS (
  SELECT
    g.FLAG_TC, g.FLAG_REENCENDIDO, g.nise_seller, g.super_grupo,
    g.dia, t.total_enc, COALESCE(c.conv_dia, 0) AS conv_dia,
    SAFE_DIVIDE(
      SUM(COALESCE(c.conv_dia, 0)) OVER (
        PARTITION BY g.FLAG_TC, g.FLAG_REENCENDIDO, g.nise_seller, g.super_grupo
        ORDER BY g.dia ROWS UNBOUNDED PRECEDING
      ), t.total_enc
    ) AS prob_D
  FROM grade g
  JOIN total_hist t
    ON  g.FLAG_TC = t.FLAG_TC AND g.FLAG_REENCENDIDO = t.FLAG_REENCENDIDO
    AND (g.nise_seller IS NOT DISTINCT FROM t.nise_seller)
    AND g.super_grupo = t.super_grupo
  LEFT JOIN conv_por_dia c
    ON  g.FLAG_TC = c.FLAG_TC AND g.FLAG_REENCENDIDO = c.FLAG_REENCENDIDO
    AND (g.nise_seller IS NOT DISTINCT FROM c.nise_seller)
    AND g.super_grupo = c.super_grupo AND g.dia = c.dia
),

nao_conv_sod AS (
  SELECT
    FLAG_TC, FLAG_REENCENDIDO, nise_seller,
    super_grupo_base AS super_grupo,
    DATE_DIFF(d.data_hoje, DT_ENCENDIDO, DAY) AS d_dec,
    SUM(QTDE) AS qtde_nc
  FROM `meli-bi-data.SBOX_CREDITSTC.proj_base_slim_gui`
  CROSS JOIN datas d
  WHERE DT_ENCENDIDO >= DATE_SUB(d.data_hoje, INTERVAL 45 DAY)
    AND DT_ENCENDIDO <  d.data_hoje
    AND (FLAG_CONVERSAO = '2. Nao Convertido'
      OR (FLAG_CONVERSAO = '1. Convertido' AND DT_CONV >= d.data_hoje))
    AND super_grupo_base != 'Mar Aberto'
  GROUP BY ALL
),

-- ==========================================================================
-- SPIKE ALIGNMENT (inalterado vs original; usa BT_CCARD_PROPOSAL)
-- ==========================================================================
spike_override AS (
  SELECT * FROM UNNEST([
    STRUCT('2026-06' AS ym, '1. TC Full'  AS FLAG_TC, 11 AS forced_peak_day),
    STRUCT('2026-06',       '2. Micro TC',            11)
  ])
),
april_daily_bcp AS (
  SELECT
    CASE WHEN CCARD_GLOBAL_LIMIT_AMT_LC <= 300 THEN '2. Micro TC' ELSE '1. TC Full' END AS FLAG_TC,
    EXTRACT(DAY FROM CAST(CCARD_PROP_UPDATE_DT AS DATE)) AS dia_num,
    COUNT(DISTINCT CUS_CUST_ID) AS conv
  FROM `meli-bi-data.WHOWNER.BT_CCARD_PROPOSAL`
  WHERE SIT_SITE_ID = 'MLB' AND CCARD_PROP_STATUS = 'accepted'
    AND DATE_TRUNC(CAST(CCARD_PROP_UPDATE_DT AS DATE), MONTH) = DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL 1 MONTH), MONTH)
  GROUP BY 1, 2
),
april_daily_smooth AS (
  SELECT FLAG_TC, dia_num,
    ROUND(AVG(conv) OVER (
      PARTITION BY FLAG_TC ORDER BY dia_num
      ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING
    )) AS conv
  FROM april_daily_bcp
),
april_last_day AS (
  SELECT FLAG_TC, conv AS conv_ultimo_dia
  FROM (
    SELECT FLAG_TC, conv, ROW_NUMBER() OVER (PARTITION BY FLAG_TC ORDER BY dia_num DESC) AS rn
    FROM april_daily_smooth
  ) WHERE rn = 1
),
hist_daily AS (
  SELECT
    CASE WHEN CCARD_GLOBAL_LIMIT_AMT_LC <= 300 THEN '2. Micro TC' ELSE '1. TC Full' END AS FLAG_TC,
    FORMAT_DATE('%Y-%m', DATE(CCARD_PROP_UPDATE_DT)) AS ym,
    EXTRACT(DAY FROM DATE(CCARD_PROP_UPDATE_DT)) AS dia_num,
    COUNT(DISTINCT CUS_CUST_ID) AS conv
  FROM `meli-bi-data.WHOWNER.BT_CCARD_PROPOSAL`
  WHERE SIT_SITE_ID='MLB' AND CCARD_PROP_STATUS='accepted'
    AND DATE(CCARD_PROP_UPDATE_DT) >= DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL 2 MONTH), MONTH)
    AND DATE(CCARD_PROP_UPDATE_DT) <  DATE_TRUNC(CURRENT_DATE(), MONTH)
  GROUP BY 1,2,3
),
hist_spikes AS (
  SELECT FLAG_TC, ym, dia_num AS dia_pico FROM (
    SELECT FLAG_TC, ym, dia_num,
      ROW_NUMBER() OVER (PARTITION BY FLAG_TC, ym ORDER BY conv DESC) AS rn
    FROM hist_daily
  ) WHERE rn = 1
),
expected_cur_spike AS (
  SELECT
    h.FLAG_TC,
    COALESCE(MAX(o.forced_peak_day), CAST(ROUND(AVG(h.dia_pico)) AS INT64)) AS spike_day
  FROM hist_spikes h
  CROSS JOIN datas d
  LEFT JOIN spike_override o
    ON o.FLAG_TC = h.FLAG_TC
   AND o.ym = FORMAT_DATE('%Y-%m', d.data_hoje)
  GROUP BY 1
),
prev_month_spike AS (
  SELECT FLAG_TC, dia_num AS spike_day FROM (
    SELECT FLAG_TC, dia_num, conv,
      ROW_NUMBER() OVER (PARTITION BY FLAG_TC ORDER BY conv DESC) AS rn
    FROM april_daily_smooth
  ) WHERE rn = 1
),
spike_offset AS (
  SELECT p.FLAG_TC, (p.spike_day - e.spike_day) AS offset_dias
  FROM prev_month_spike p JOIN expected_cur_spike e USING(FLAG_TC)
),
proj_template AS (
  SELECT
    s.FLAG_TC,
    'BAU' AS super_grupo,
    DATE(EXTRACT(YEAR FROM d.data_hoje), EXTRACT(MONTH FROM d.data_hoje), k) AS data_proj,
    COALESCE(ROUND(a.conv), ROUND(l.conv_ultimo_dia)) AS proj_dia
  FROM spike_offset s
  CROSS JOIN datas d,
  UNNEST(GENERATE_ARRAY(EXTRACT(DAY FROM d.data_hoje), EXTRACT(DAY FROM d.fim_mes))) AS k
  LEFT JOIN april_daily_smooth a
    ON a.FLAG_TC = s.FLAG_TC AND a.dia_num = k + s.offset_dias
  LEFT JOIN april_last_day l ON l.FLAG_TC = s.FLAG_TC
),

future_grid AS (
  SELECT k, DATE_ADD(d.data_hoje, INTERVAL k DAY) AS data_proj
  FROM datas d,
  UNNEST(GENERATE_ARRAY(0, DATE_DIFF(d.fim_mes, d.data_hoje, DAY))) AS k
),

proj_organico AS (
  SELECT
    n.FLAG_TC, n.super_grupo, f.data_proj,
    SUM(ROUND(n.qtde_nc * SAFE_DIVIDE(
      COALESCE(p_hj.prob_D, 0) - COALESCE(p_on.prob_D, 0),
      1 - COALESCE(p_on.prob_D, 0)
    ))) AS proj_dia
  FROM nao_conv_sod n
  CROSS JOIN future_grid f
  LEFT JOIN curva p_hj
    ON  n.FLAG_TC          = p_hj.FLAG_TC
    AND n.FLAG_REENCENDIDO = p_hj.FLAG_REENCENDIDO
    AND (n.nise_seller IS NOT DISTINCT FROM p_hj.nise_seller)
    AND n.super_grupo      = p_hj.super_grupo
    AND (n.d_dec + f.k)    = p_hj.dia
  LEFT JOIN curva p_on
    ON  n.FLAG_TC              = p_on.FLAG_TC
    AND n.FLAG_REENCENDIDO     = p_on.FLAG_REENCENDIDO
    AND (n.nise_seller IS NOT DISTINCT FROM p_on.nise_seller)
    AND n.super_grupo          = p_on.super_grupo
    AND (n.d_dec + f.k - 1)   = p_on.dia
  GROUP BY 1, 2, 3
),

ma_enc_hist AS (
  SELECT FLAG_TC, DATE_TRUNC(DT_ENCENDIDO, MONTH) AS mes, SUM(QTDE) AS enc_ma
  FROM `meli-bi-data.SBOX_CREDITSTC.proj_base_slim_gui`
  WHERE super_grupo_base = 'Mar Aberto'
    AND DT_ENCENDIDO >= DATE_TRUNC(DATE_SUB(DATE_TRUNC(CURRENT_DATE(), MONTH), INTERVAL 3 MONTH), MONTH)
    AND DT_ENCENDIDO <  DATE_TRUNC(CURRENT_DATE(), MONTH)
    AND EXTRACT(MONTH FROM DT_ENCENDIDO) NOT IN (11, 12)
  GROUP BY 1, 2
),
ma_conv_hist AS (
  SELECT FLAG_TC, DATE_TRUNC(DT_CONV, MONTH) AS mes, SUM(QTDE) AS conv_ma
  FROM `meli-bi-data.SBOX_CREDITSTC.proj_base_slim_gui`
  WHERE FLAG_CONVERSAO = '1. Convertido'
    AND super_grupo_base = 'Mar Aberto'
    AND DT_CONV >= DATE_TRUNC(DATE_SUB(DATE_TRUNC(CURRENT_DATE(), MONTH), INTERVAL 3 MONTH), MONTH)
    AND DT_CONV <  DATE_TRUNC(CURRENT_DATE(), MONTH)
    AND EXTRACT(MONTH FROM DT_CONV) NOT IN (11, 12)
  GROUP BY 1, 2
),
ma_taxa_hist AS (
  SELECT e.FLAG_TC, AVG(SAFE_DIVIDE(c.conv_ma, e.enc_ma)) AS taxa_ma
  FROM ma_enc_hist e LEFT JOIN ma_conv_hist c USING (FLAG_TC, mes)
  GROUP BY 1
),
ma_enc_atual AS (
  SELECT FLAG_TC, SUM(QTDE) AS enc_so_far
  FROM `meli-bi-data.SBOX_CREDITSTC.proj_base_slim_gui`
  CROSS JOIN datas
  WHERE super_grupo_base = 'Mar Aberto'
    AND DATE_TRUNC(DT_ENCENDIDO, MONTH) = DATE_TRUNC(CURRENT_DATE(), MONTH)
    AND DT_ENCENDIDO < data_hoje
  GROUP BY 1
),
ma_est_enc_total AS (
  SELECT e.FLAG_TC,
    ROUND(e.enc_so_far * SAFE_DIVIDE(
      DATE_DIFF(d.fim_mes, d.inicio_mes, DAY) + 1,
      GREATEST(DATE_DIFF(d.data_hoje, d.inicio_mes, DAY), 1)
    )) AS est_enc_total
  FROM ma_enc_atual e CROSS JOIN datas d
),
ma_real AS (
  SELECT FLAG_TC, SUM(QTDE) AS ma_conv_real
  FROM `meli-bi-data.SBOX_CREDITSTC.proj_base_slim_gui`
  CROSS JOIN datas
  WHERE FLAG_CONVERSAO = '1. Convertido'
    AND super_grupo_base = 'Mar Aberto'
    AND DATE_TRUNC(DT_CONV, MONTH) = DATE_TRUNC(CURRENT_DATE(), MONTH)
    AND DT_CONV < data_hoje
  GROUP BY 1
),
ma_emit_c AS (
  SELECT t.FLAG_TC,
    GREATEST(ROUND(t.est_enc_total * h.taxa_ma) - COALESCE(r.ma_conv_real, 0), 0) AS emit_c_remaining,
    DATE_DIFF(d.fim_mes, d.data_hoje, DAY) + 1 AS days_remaining
  FROM ma_est_enc_total t
  JOIN ma_taxa_hist h USING (FLAG_TC)
  LEFT JOIN ma_real r USING (FLAG_TC)
  CROSS JOIN datas d
),
ma_proj AS (
  SELECT m.FLAG_TC, 'Mar Aberto' AS super_grupo, f.data_proj,
    ROUND(m.emit_c_remaining / GREATEST(m.days_remaining, 1)) AS proj_dia
  FROM ma_emit_c m CROSS JOIN future_grid f
),

past AS (
  -- Realizado diario (convertidos). Janela = mes vigente + 5 meses anteriores
  -- (safra de 6 meses na aba Projecao). Antes era INTERVAL 1 MONTH (so vigente+1).
  SELECT
    FLAG_TC,
    CASE WHEN flag_ea THEN 'EA' ELSE super_grupo_base END AS super_grupo,
    DT_CONV AS dia,
    SUM(QTDE) AS total
  FROM `meli-bi-data.SBOX_CREDITSTC.proj_base_slim_gui`
  WHERE FLAG_CONVERSAO = '1. Convertido'
    AND DT_CONV >= DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL 5 MONTH), MONTH)
    AND DT_CONV <  CURRENT_DATE()
  GROUP BY ALL
)

SELECT FLAG_TC, super_grupo, CAST(dia AS STRING) AS dia, SUM(total) AS total, tipo
FROM (
  SELECT FLAG_TC, super_grupo, dia, total, 'actual' AS tipo FROM past
  UNION ALL
  SELECT FLAG_TC, super_grupo, data_proj, proj_dia, 'proj' AS tipo FROM proj_template
  UNION ALL
  SELECT FLAG_TC, super_grupo, data_proj, proj_dia, 'proj' AS tipo FROM proj_organico WHERE super_grupo != 'BAU'
  UNION ALL
  SELECT FLAG_TC, super_grupo, data_proj, proj_dia, 'proj' AS tipo FROM ma_proj
)
GROUP BY 1, 2, 3, 5
ORDER BY FLAG_TC, super_grupo, dia
