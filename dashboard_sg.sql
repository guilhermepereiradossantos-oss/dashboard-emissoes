-- ============================================================
-- DASHBOARD DATA (por super_grupo)
-- Retorna: FLAG_TC, super_grupo, dia, total, tipo
-- tipo = 'actual' | 'proj'
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
    FLAG_TC, FLAG_REENCENDIDO,
    CASE WHEN FLAG_SELLERS IN ('SELLER','MIXTO') THEN 'SELLER' ELSE FLAG_NISE END AS nise_seller,
    APP_INST,
    CASE
      WHEN FLAG_SELLERS IN ('SELLER','MIXTO')                    THEN 'Sellers'
      WHEN grupo_especial LIKE '%Mar Aberto%'                    THEN 'Mar Aberto'
      WHEN grupo_especial = 'TEST REACH-TEST NO ECOSISTEMATICOS' THEN 'Only Nav'
      WHEN grupo_especial LIKE '%CANCELADAS%'                    THEN 'Cuentas Canceladas'
      ELSE 'BAU'
    END AS super_grupo,
    PLACEMENT, FLAG_CONVERSAO, DIAS_CONV, QTDE
  FROM `meli-bi-data.SBOX_CREDITSTC.base_projecao_emissao_igor`
  WHERE DT_ENCENDIDO >= DATE_TRUNC(DATE_SUB(DATE_TRUNC(CURRENT_DATE(), MONTH), INTERVAL 3 MONTH), MONTH)
    AND DT_ENCENDIDO <  DATE_TRUNC(CURRENT_DATE(), MONTH)
    AND EXTRACT(MONTH FROM DT_ENCENDIDO) NOT IN (11, 12)
),
total_hist AS (
  SELECT FLAG_TC, FLAG_REENCENDIDO, nise_seller, APP_INST, super_grupo, SUM(QTDE) AS total_enc
  FROM hist GROUP BY ALL
),
conv_por_dia AS (
  SELECT FLAG_TC, FLAG_REENCENDIDO, nise_seller, APP_INST, super_grupo,
         DIAS_CONV AS dia,
         SUM(CASE WHEN FLAG_CONVERSAO = '1. Convertido'
                   AND COALESCE(PLACEMENT,'') != 'EA' THEN QTDE ELSE 0 END) AS conv_dia
  FROM hist
  WHERE DIAS_CONV IS NOT NULL AND DIAS_CONV <= 59
  GROUP BY ALL
),
grade AS (
  SELECT t.FLAG_TC, t.FLAG_REENCENDIDO, t.nise_seller, t.APP_INST, t.super_grupo, d AS dia
  FROM total_hist t CROSS JOIN UNNEST(GENERATE_ARRAY(0, 59)) AS d
),
curva AS (
  SELECT
    g.FLAG_TC, g.FLAG_REENCENDIDO, g.nise_seller, g.APP_INST, g.super_grupo,
    g.dia, t.total_enc, COALESCE(c.conv_dia, 0) AS conv_dia,
    SAFE_DIVIDE(
      SUM(COALESCE(c.conv_dia, 0)) OVER (
        PARTITION BY g.FLAG_TC, g.FLAG_REENCENDIDO, g.nise_seller, g.APP_INST, g.super_grupo
        ORDER BY g.dia ROWS UNBOUNDED PRECEDING
      ), t.total_enc
    ) AS prob_D
  FROM grade g
  JOIN total_hist t
    ON  g.FLAG_TC = t.FLAG_TC AND g.FLAG_REENCENDIDO = t.FLAG_REENCENDIDO
    AND (g.nise_seller IS NOT DISTINCT FROM t.nise_seller)
    AND (g.APP_INST    IS NOT DISTINCT FROM t.APP_INST)
    AND g.super_grupo = t.super_grupo
  LEFT JOIN conv_por_dia c
    ON  g.FLAG_TC = c.FLAG_TC AND g.FLAG_REENCENDIDO = c.FLAG_REENCENDIDO
    AND (g.nise_seller IS NOT DISTINCT FROM c.nise_seller)
    AND (g.APP_INST    IS NOT DISTINCT FROM c.APP_INST)
    AND g.super_grupo = c.super_grupo AND g.dia = c.dia
),

nao_conv_sod AS (
  SELECT
    FLAG_TC, FLAG_REENCENDIDO,
    CASE WHEN FLAG_SELLERS IN ('SELLER','MIXTO') THEN 'SELLER' ELSE FLAG_NISE END AS nise_seller,
    APP_INST,
    CASE
      WHEN FLAG_SELLERS IN ('SELLER','MIXTO')                    THEN 'Sellers'
      WHEN grupo_especial LIKE '%Mar Aberto%'                    THEN 'Mar Aberto'
      WHEN grupo_especial = 'TEST REACH-TEST NO ECOSISTEMATICOS' THEN 'Only Nav'
      WHEN grupo_especial LIKE '%CANCELADAS%'                    THEN 'Cuentas Canceladas'
      ELSE 'BAU'
    END AS super_grupo,
    DATE_DIFF(d.data_hoje, DT_ENCENDIDO, DAY) AS d_dec,
    SUM(QTDE) AS qtde_nc
  FROM `meli-bi-data.SBOX_CREDITSTC.base_projecao_emissao_igor`
  CROSS JOIN datas d
  WHERE DT_ENCENDIDO >= DATE_SUB(d.data_hoje, INTERVAL 45 DAY)
    AND DT_ENCENDIDO <  d.data_hoje
    AND (FLAG_CONVERSAO = '2. Nao Convertido'
      OR (FLAG_CONVERSAO = '1. Convertido' AND DT_CONV >= d.data_hoje))
    AND NOT (FLAG_SELLERS NOT IN ('SELLER','MIXTO') AND grupo_especial LIKE '%Mar Aberto%')
  GROUP BY ALL
),

-- Volume de ativacoes do mes atual vs mes anterior para detectar se encendimento ocorreu
qtde_enc_mes_atual AS (
  SELECT COALESCE(SUM(QTDE), 0) AS n
  FROM `meli-bi-data.SBOX_CREDITSTC.base_projecao_emissao_igor`
  CROSS JOIN datas d
  WHERE DATE_TRUNC(DT_ENCENDIDO, MONTH) = DATE_TRUNC(d.data_hoje, MONTH)
),
qtde_enc_mes_prev AS (
  SELECT COALESCE(SUM(QTDE), 0) AS n
  FROM `meli-bi-data.SBOX_CREDITSTC.base_projecao_emissao_igor`
  CROSS JOIN datas d
  WHERE DATE_TRUNC(DT_ENCENDIDO, MONTH) = DATE_TRUNC(DATE_SUB(d.data_hoje, INTERVAL 1 MONTH), MONTH)
    AND EXTRACT(MONTH FROM DT_ENCENDIDO) NOT IN (11, 12)
),

-- Template de conversoes do mes anterior dia a dia (quando encendimento ainda nao ocorreu)
-- Usa os valores REAIS de conversao do mes passado como referencia para a projecao
prev_daily_template AS (
  SELECT
    FLAG_TC,
    CASE
      WHEN COALESCE(PLACEMENT,'') = 'EA'                         THEN 'EA'
      WHEN FLAG_SELLERS IN ('SELLER','MIXTO')                    THEN 'Sellers'
      WHEN grupo_especial LIKE '%Mar Aberto%'                    THEN 'Mar Aberto'
      WHEN grupo_especial = 'TEST REACH-TEST NO ECOSISTEMATICOS' THEN 'Only Nav'
      WHEN grupo_especial LIKE '%CANCELADAS%'                    THEN 'Cuentas Canceladas'
      ELSE 'BAU'
    END AS super_grupo,
    EXTRACT(DAY FROM DT_CONV) AS dia_num,
    SUM(QTDE) AS conversoes
  FROM `meli-bi-data.SBOX_CREDITSTC.base_projecao_emissao_igor`
  CROSS JOIN datas d
  WHERE FLAG_CONVERSAO = '1. Convertido'
    AND DATE_TRUNC(DT_CONV, MONTH) = DATE_TRUNC(DATE_SUB(d.data_hoje, INTERVAL 1 MONTH), MONTH)
    AND EXTRACT(MONTH FROM DT_CONV) NOT IN (11, 12)
  GROUP BY ALL
),

-- Conversoes diarias de abril via BT_CCARD_PROPOSAL (fonte de verdade para alinhamento)
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

-- Suaviza o template de abril com media movel de 3 dias
-- Remove bumps secundarios pontuais (ex: segundo pico de Micro TC em abr/15)
-- para evitar que anomalias de abril sejam projetadas em maio
april_daily_smooth AS (
  SELECT FLAG_TC, dia_num,
    ROUND(AVG(conv) OVER (
      PARTITION BY FLAG_TC
      ORDER BY dia_num
      ROWS BETWEEN 1 PRECEDING AND 1 FOLLOWING
    )) AS conv
  FROM april_daily_bcp
),

-- Ultimo dia disponivel de abril por FLAG_TC (fallback para dias alem de 30)
april_last_day AS (
  SELECT FLAG_TC, conv AS conv_ultimo_dia
  FROM (
    SELECT FLAG_TC, conv, ROW_NUMBER() OVER (PARTITION BY FLAG_TC ORDER BY dia_num DESC) AS rn
    FROM april_daily_smooth
  ) WHERE rn = 1
),

-- ==========================================================================
-- SPIKE ALIGNMENT (refeito 03/06/2026)
-- Objetivo: replicar shape do mes anterior, alinhado pelo pico esperado do
-- mes corrente. Pico esperado vem de auto-detect (media dos 2 ultimos meses)
-- ou override de evento (EA drops planejados, etc.).
-- ==========================================================================

-- CONFIG: overrides de pico para eventos especiais.
-- Edite essa CTE quando souber a data exata do EA drop ou similar.
-- Sem override = auto-detect (media dos 2 meses anteriores).
spike_override AS (
  SELECT * FROM UNNEST([
    STRUCT('2026-06' AS ym, '1. TC Full'  AS FLAG_TC, 11 AS forced_peak_day),
    STRUCT('2026-06',       '2. Micro TC',            11)
  ])
),

-- Volumes diarios (BCP, ultimos 2 meses)
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

-- Pico (dia do mes) dos 2 meses anteriores
hist_spikes AS (
  SELECT FLAG_TC, ym, dia_num AS dia_pico FROM (
    SELECT FLAG_TC, ym, dia_num,
      ROW_NUMBER() OVER (PARTITION BY FLAG_TC, ym ORDER BY conv DESC) AS rn
    FROM hist_daily
  ) WHERE rn = 1
),

-- Pico esperado do mes corrente: override > media dos 2 ultimos meses
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

-- Pico do mes IMEDIATAMENTE anterior, usando o dado SUAVIZADO (que e
-- o que o proj_template efetivamente aplica). Garante que o pico do
-- shape alinhado caia exatamente no expected_cur_spike.
prev_month_spike AS (
  SELECT FLAG_TC, dia_num AS spike_day FROM (
    SELECT FLAG_TC, dia_num, conv,
      ROW_NUMBER() OVER (PARTITION BY FLAG_TC ORDER BY conv DESC) AS rn
    FROM april_daily_smooth
  ) WHERE rn = 1
),

-- offset = spike_mes_anterior - spike_esperado_mes_corrente
-- Pra mes corrente dia k: olha mes anterior dia (k + offset)
-- Ex: prev_spike=7, expected_cur_spike=11 -> offset=-4
--     -> mes corrente dia 11 = mes anterior dia 7 (o pico)
spike_offset AS (
  SELECT p.FLAG_TC, (p.spike_day - e.spike_day) AS offset_dias
  FROM prev_month_spike p JOIN expected_cur_spike e USING(FLAG_TC)
),

-- Projecao alinhada pelo pico: replica o shape de abril pos-encendimento
-- alinhado pela posicao do pico, nao pelo dia do mes
proj_template AS (
  SELECT
    s.FLAG_TC,
    'BAU' AS super_grupo,
    DATE(EXTRACT(YEAR FROM d.data_hoje), EXTRACT(MONTH FROM d.data_hoje), k) AS data_proj,
    COALESCE(
      ROUND(a.conv),           -- abril no dia alinhado
      ROUND(l.conv_ultimo_dia) -- fallback: ultimo dia de abril
    ) AS proj_dia
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
    n.FLAG_TC,
    n.super_grupo,
    f.data_proj,
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
    AND (n.APP_INST    IS NOT DISTINCT FROM p_hj.APP_INST)
    AND n.super_grupo      = p_hj.super_grupo
    AND (n.d_dec + f.k)    = p_hj.dia
  LEFT JOIN curva p_on
    ON  n.FLAG_TC              = p_on.FLAG_TC
    AND n.FLAG_REENCENDIDO     = p_on.FLAG_REENCENDIDO
    AND (n.nise_seller IS NOT DISTINCT FROM p_on.nise_seller)
    AND (n.APP_INST    IS NOT DISTINCT FROM p_on.APP_INST)
    AND n.super_grupo          = p_on.super_grupo
    AND (n.d_dec + f.k - 1)   = p_on.dia
  GROUP BY 1, 2, 3
),

ma_enc_hist AS (
  SELECT FLAG_TC, DATE_TRUNC(DT_ENCENDIDO, MONTH) AS mes, SUM(QTDE) AS enc_ma
  FROM `meli-bi-data.SBOX_CREDITSTC.base_projecao_emissao_igor`
  WHERE FLAG_SELLERS NOT IN ('SELLER','MIXTO') AND grupo_especial LIKE '%Mar Aberto%'
    AND DT_ENCENDIDO >= DATE_TRUNC(DATE_SUB(DATE_TRUNC(CURRENT_DATE(), MONTH), INTERVAL 3 MONTH), MONTH)
    AND DT_ENCENDIDO <  DATE_TRUNC(CURRENT_DATE(), MONTH)
    AND EXTRACT(MONTH FROM DT_ENCENDIDO) NOT IN (11, 12)
  GROUP BY 1, 2
),
ma_conv_hist AS (
  SELECT FLAG_TC, DATE_TRUNC(DT_CONV, MONTH) AS mes, SUM(QTDE) AS conv_ma
  FROM `meli-bi-data.SBOX_CREDITSTC.base_projecao_emissao_igor`
  WHERE FLAG_CONVERSAO = '1. Convertido'
    AND FLAG_SELLERS NOT IN ('SELLER','MIXTO') AND grupo_especial LIKE '%Mar Aberto%'
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
  FROM `meli-bi-data.SBOX_CREDITSTC.base_projecao_emissao_igor`
  CROSS JOIN datas
  WHERE FLAG_SELLERS NOT IN ('SELLER','MIXTO') AND grupo_especial LIKE '%Mar Aberto%'
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
  FROM `meli-bi-data.SBOX_CREDITSTC.base_projecao_emissao_igor`
  CROSS JOIN datas
  WHERE FLAG_CONVERSAO = '1. Convertido'
    AND FLAG_SELLERS NOT IN ('SELLER','MIXTO') AND grupo_especial LIKE '%Mar Aberto%'
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

-- ==========================================================================
-- past = realizados ate ontem.
-- Fonte primaria: base_projecao_emissao_igor (tem quebra por super_grupo).
-- TEMPORARIO (09/06/2026): o pipeline do Igor falha desde 04/06 com policy
-- tag error em BT_REG_MONEY_PLUS.REG_IDENTIFICATION_NUMBER. Enquanto nao
-- temos base propria, fallback pra BCP (BT_CCARD_PROPOSAL) nos dias onde
-- base_projecao tem < 50% do volume do BCP. Pra dias fallback, tudo em BAU
-- (BCP nao tem dim de super_grupo). Remover esse fallback quando a base
-- propria estiver pronta.
-- ==========================================================================
past_proj AS (
  SELECT
    FLAG_TC,
    CASE
      WHEN COALESCE(PLACEMENT,'') = 'EA'                         THEN 'EA'
      WHEN FLAG_SELLERS IN ('SELLER','MIXTO')                    THEN 'Sellers'
      WHEN grupo_especial LIKE '%Mar Aberto%'                    THEN 'Mar Aberto'
      WHEN grupo_especial = 'TEST REACH-TEST NO ECOSISTEMATICOS' THEN 'Only Nav'
      WHEN grupo_especial LIKE '%CANCELADAS%'                    THEN 'Cuentas Canceladas'
      ELSE 'BAU'
    END AS super_grupo,
    DT_CONV AS dia,
    SUM(QTDE) AS total
  FROM `meli-bi-data.SBOX_CREDITSTC.base_projecao_emissao_igor`
  WHERE FLAG_CONVERSAO = '1. Convertido'
    AND DT_CONV >= DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL 1 MONTH), MONTH)
    AND DT_CONV <  CURRENT_DATE()
  GROUP BY ALL
),
past_proj_day_tot AS (
  SELECT FLAG_TC, dia, SUM(total) AS proj_total FROM past_proj GROUP BY 1, 2
),
past_bcp_day_tot AS (
  SELECT
    CASE WHEN CCARD_GLOBAL_LIMIT_AMT_LC <= 300 THEN '2. Micro TC' ELSE '1. TC Full' END AS FLAG_TC,
    DATE(CCARD_PROP_UPDATE_DT) AS dia,
    COUNT(*) AS bcp_total
  FROM `meli-bi-data.WHOWNER.BT_CCARD_PROPOSAL`
  WHERE SIT_SITE_ID='MLB' AND CCARD_PROP_STATUS='accepted'
    AND DATE(CCARD_PROP_UPDATE_DT) >= DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL 1 MONTH), MONTH)
    AND DATE(CCARD_PROP_UPDATE_DT) <  CURRENT_DATE()
  GROUP BY 1, 2
),
stale_days AS (
  SELECT b.FLAG_TC, b.dia, b.bcp_total
  FROM past_bcp_day_tot b
  LEFT JOIN past_proj_day_tot p USING (FLAG_TC, dia)
  WHERE COALESCE(p.proj_total, 0) < 0.5 * b.bcp_total
),
past AS (
  SELECT p.FLAG_TC, p.super_grupo, p.dia, p.total
  FROM past_proj p
  LEFT JOIN stale_days s USING (FLAG_TC, dia)
  WHERE s.dia IS NULL
  UNION ALL
  SELECT FLAG_TC, 'BAU' AS super_grupo, dia, bcp_total AS total
  FROM stale_days
),

-- Ajuste manual: -15k TC Full distribuidos nos dias restantes do mes
-- Motivo: ~832k propostas pendentes canceladas em 19/Mai que o modelo
-- ainda conta como conversores potenciais (base_projecao nao reflete BCP)
ajuste_cancel_tc_full AS (
  SELECT
    '1. TC Full' AS FLAG_TC,
    'BAU'        AS super_grupo,
    f.data_proj,
    ROUND(-15000.0 / (DATE_DIFF(d.fim_mes, d.data_hoje, DAY) + 1)) AS proj_dia
  FROM future_grid f CROSS JOIN datas d
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
  UNION ALL
  SELECT FLAG_TC, super_grupo, data_proj, proj_dia, 'proj' AS tipo FROM ajuste_cancel_tc_full
)
GROUP BY 1, 2, 3, 5
ORDER BY FLAG_TC, super_grupo, dia
