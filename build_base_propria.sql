-- ==========================================================================
-- base_projecao_emissao_guilherme
--
-- Clone da base_projecao_emissao_igor SEM a dependencia do BT_REG_MONEY_PLUS
-- (REG_IDENTIFICATION_NUMBER tem policy tag que esta quebrando o pipeline do
-- Igor desde 04/06/2026).
--
-- Mudancas vs SQL do Igor:
--   1. CTE `scr` REMOVIDA (vinha de BT_REG_MONEY_PLUS)
--   2. JOIN `kyc` REMOVIDO (era usado apenas pra ligar com scr)
--   3. JOIN com `scr` REMOVIDO
--   4. Colunas SCR-dependentes setadas como NULL:
--      - NOVO_LIM_SCR -> NULL  (afeta alcance_limite_tc_vs_limite_scr)
--      - limite_total -> NULL  (afeta faixa_uso_tc_fora, faixa_limite_total)
--      - saldo_utilizado -> NULL (afeta determinante_atendimento, faixa_atendimento, faixa_uso_tc_fora)
--      - REG_QTDEINSTITUICOES -> NULL  (afeta range_qtd_instit)
--      - REG_IDENTIFICATION_NUMBER -> NULL  (afeta alcance_limite_tc_vs_limite_scr, status_scr)
--      - REG_DATA_BASE -> NULL
--
-- Impacto no dashboard-emissoes: ZERO. As dimensoes SCR-dependentes nao sao
-- usadas pelo dashboard. As unicas dims que usamos sao: FLAG_NISE, FLAG_SELLERS,
-- FLAG_APP_ATIVO, rating_tc, range_bureau, grupo_especial, PLACEMENT, DT_ENCENDIDO,
-- DT_CONV, FLAG_TC, FLAG_CONVERSAO, QTDE, FLAG_REENCENDIDO, DIAS_CONV, APP_INST.
-- ==========================================================================

CREATE OR REPLACE TABLE `meli-bi-data.SBOX_CREDITSTC.base_projecao_emissao_guilherme` AS
WITH

proposal_ajustada AS (
  SELECT
    CAST(A.CCARD_PROP_CREATION_DT AS DATE) as DT_ENCENDIDO,
    case when CAST(A.CCARD_PROP_CREATION_DT AS DATE) < '2023-09-01' then '2023-09-01' else CAST(A.CCARD_PROP_CREATION_DT AS DATE) end as DT_ENCENDIDO_NISE,
    ccard_prop_status,
    CAST(A.CCARD_PROP_UPDATE_DT AS DATE) as DT_CONV,
    safe_cast(A.CUS_CUST_ID as int64) as cus_cust_id,
    a.ccard_prop_id,
    CASE WHEN A.CCARD_GLOBAL_LIMIT_AMT_LC <= 100 or CCARD_PRODUCT_ID = 5 THEN '2. Micro TC' ELSE '1. TC Full' END AS FLAG_TC,
    CASE WHEN A.CCARD_PROP_STATUS = 'accepted' then '1. Convertido' else '2. Nao Convertido' end as FLAG_CONVERSAO,
    CASE WHEN A.CCARD_PROP_STATUS = 'accepted' then DATE_DIFF(CAST(A.CCARD_PROP_UPDATE_DT AS DATE) , CAST(A.CCARD_PROP_CREATION_DT AS DATE), DAY) else null end  DIAS_CONV,
    case
      when  lag(CCARD_GLOBAL_LIMIT_AMT_LC,1) over(partition by cus_cust_id ORDER BY  ccard_prop_creation_dT asc) > CCARD_GLOBAL_LIMIT_AMT_LC then 'Downsell'
      when  lag(CCARD_GLOBAL_LIMIT_AMT_LC,1) over(partition by cus_cust_id ORDER BY  ccard_prop_creation_dT asc) = CCARD_GLOBAL_LIMIT_AMT_LC then 'Mesmo Limite que Anterior'
      when  lag(CCARD_GLOBAL_LIMIT_AMT_LC,1) over(partition by cus_cust_id ORDER BY  ccard_prop_creation_dT asc) < CCARD_GLOBAL_LIMIT_AMT_LC then 'Upsell'
      when lag(CCARD_GLOBAL_LIMIT_AMT_LC,1) over(partition by cus_cust_id ORDER BY  ccard_prop_creation_dT asc) is null then 'Primeira Proposta'
    end status_limite_reenc,
    case when
      min(case when ccard_prop_status = 'accepted' then cast(ccard_prop_creation_dT as date) else '2099-01-01' end) over(partition by cus_cust_id order by ccard_prop_creation_dt asc) < CAST(A.CCARD_PROP_CREATION_Dt AS DATE)
      then true else false end status_cancelada_anteriormente,
    lag(ccard_prop_creation_dt,1) over(partition by cus_cust_id ORDER BY  ccard_prop_creation_dt asc) as data_ultimo_encendido,
    lag(CCARD_PROP_UPDATE_Dt,1) over(partition by cus_cust_id ORDER BY  ccard_prop_creation_dt asc)as data_ultimo_apagado,
    A.CCARD_GLOBAL_LIMIT_AMT_LC as ccard_global_limit_amt_lc,
    lag(CCARD_GLOBAL_LIMIT_AMT_LC,1) over(partition by cus_cust_id ORDER BY  ccard_prop_creation_dt asc)  as limite_anterior
  FROM `meli-bi-data.WHOWNER.BT_CCARD_PROPOSAL` A
  where sit_site_id = 'MLB'
    -- Otimizacao agressiva: dashboard so usa ultimos 3 meses. Pegando 6 meses
    -- de margem pra LAG (que depende de propostas anteriores). Reduz drasticamente
    -- volume processado vs Igor que vai ate 2021.
    and CAST(A.CCARD_PROP_CREATION_DT AS DATE) >= DATE_SUB(CURRENT_DATE(), INTERVAL 6 MONTH)
),

proposal AS (
  SELECT A.*, ROW_NUMBER() OVER(PARTITION BY CUS_CUST_ID ORDER BY DT_ENCENDIDO ASC) AS NUMERO_PROP
  FROM proposal_ajustada A
),

app_instalado AS (
  SELECT DISTINCT B1.CUS_CUST_ID,
    string_agg(distinct marketplace order by marketplace asc) AS APP_INST
  FROM meli-bi-data.WHOWNER.BT_MKT_OWNCHANNELS_DEVICES B1
  WHERE UPPER(B1.PLATFORM) IN ('IOS', 'ANDROID')
    AND UPPER(B1.STATUS) = 'ACTIVE'
    AND UPPER(B1.SIT_SITE_ID) ='MLB'
    and marketplace IN ('MERCADOPAGO','MERCADOLIBRE','MERCADOLIVRE')
  group by 1
),

tc_fisica AS (
  SELECT CUS_CUST_ID, CREATED_DATE, ORIGEM_TABLE
  FROM (
    SELECT REQ.CUS_CUST_ID,
      'FISICA_DEBITO_HIBRIDA' AS PPD_CARD_TYPE_GROUP,
      CAST(DBT_CARD_REQ_DATE_CREATED AS DATE) AS CREATED_DATE,
      'TB_REQUEST' as ORIGEM_TABLE
    FROM meli-bi-data.WHOWNER.BT_DBT_CARD_REQUEST REQ
    WHERE REQ.SIT_SITE_ID like '%MLB%'
    UNION DISTINCT
    SELECT C.CUS_CUST_ID,
      'FISICA_DEBITO_HIBRIDA' AS PPD_CARD_TYPE_GROUP,
      CAST(C.CARD_CREATION_DT AS DATE) AS CREATED_DATE,
      'TB_MP_CARD' as ORIGEM_TABLE
    FROM meli-bi-data.WHOWNER.LK_MP_CARD C
    WHERE C.sit_site_id='MLB' and C.CARD_TYPE_GROUP ='FISICA_DEBITO_HIBRIDA'
  )
  QUALIFY row_number() over (partition by cus_cust_id order by CAST(CREATED_DATE AS DATE)) = 1
),

prop_cc AS (
  SELECT DISTINCT (cus_cust_id_borrower) as CUS_CUST_ID,
    CRD_PROP_CREATION_DATE_ID,
    CRD_PROP_TOTAL_AMOUNT
  FROM `meli-bi-data.WHOWNER.BT_MP_CREDITS_PROPOSAL_DETAIL`
  WHERE upper(sit_site_id) = 'MLB'
    and upper(crd_prop_purpose_id) = 'PURCHASE'
    and upper(crd_prop_status_id) in ('APPROVED')
  QUALIFY row_number() over(partition by cus_cust_id_borrower order by crd_prop_creation_date_id desc) =1
),

uso_cc AS (
  SELECT
    TT.cus_cust_id_borrower AS CUS_CUST_ID,
    CASE WHEN TT.TPV_SEGMENT_GRP = 'PERSONAL LOAN' THEN 'PL' ELSE 'BNPL' END AS PROD_CC,
    CAST(TT.CRD_CREDIT_CREATION_DATE_ID AS DATE) AS CRD_CREDIT_CREATION_DATE_ID,
    coalesce(lag(CAST(TT.CRD_CREDIT_CREATION_DATE_ID AS DATE)) over(partition by cus_cust_id_borrower order by CAST(TT.CRD_CREDIT_CREATION_DATE_ID AS DATE) desc),'2999-01-01') as prox_uso_cc
  FROM `meli-bi-data.WHOWNER.BT_MP_CREDITS_CREDIT_DETAIL` AS TT
  WHERE TT.sit_site_id = 'MLB'
    and TT.crd_credit_status_id not in ('CANCELLED','ANNULLED','DEFAULTED')
    and upper(TT.crd_prop_purpose_id) = 'PURCHASE'
),

-- NOTE: CTE `scr` REMOVIDA (vinha de BT_REG_MONEY_PLUS com policy tag)

proxy_decil AS (
  SELECT
    CASE WHEN CAST(CRD_PERCENTILE AS INT) <= 10 THEN 1
      WHEN CAST(CRD_PERCENTILE AS INT) <= 20 THEN 2
      WHEN CAST(CRD_PERCENTILE AS INT) <= 30 THEN 3
      WHEN CAST(CRD_PERCENTILE AS INT) <= 40 THEN 4
      WHEN CAST(CRD_PERCENTILE AS INT) <= 50 THEN 5
      WHEN CAST(CRD_PERCENTILE AS INT) <= 60 THEN 6
      WHEN CAST(CRD_PERCENTILE AS INT) <= 70 THEN 7
      WHEN CAST(CRD_PERCENTILE AS INT) <= 80 THEN 8
      WHEN CAST(CRD_PERCENTILE AS INT) <= 90 THEN 9
      WHEN CAST(CRD_PERCENTILE AS INT) <= 100 THEN 10 END AS DECIL,
    CUS_CUST_ID_BORROWER AS CUS_CUST_ID,
    CREATION_DATE as creation_date_dt,
    format_date('%Y%m',CREATION_DATE) as anomes_Decil,
    CRD_MODEL
  FROM `meli-bi-data.WHOWNER.BT_CRD_CREDITS_ML_SCORING`
  WHERE SIT_SITE_ID = 'MLB'
    AND CRD_MODEL = 'consumers_conversion_credit_card'
    and CREATION_DATE >= '2023-01-01'
),

decil_convs AS (
  SELECT decil, cus_cust_id, crd_model,
    creation_date_dt as fecha_decil_inicio,
    lag(creation_date_dt,1,'2099-01-01') over(partition by cus_cust_id ORDER BY creation_date_dt desc) as fecha_decil_fim
  FROM proxy_decil dc
),

uso_ccard AS (
  SELECT DISTINCT pur.cus_cust_id, ccard_prop_id,
    cast(CCARD_PURCH_OP_DT as date) as data_uso_ccard,
    'Com Uso' as status_uso_ccard
  FROM `meli-bi-data.WHOWNER.BT_CCARD_PURCHASE` pur
  LEFT JOIN `meli-bi-data.WHOWNER.BT_CCARD_ACCOUNT` acc
    ON pur.cus_cust_id =acc.cus_cust_id AND pur.CCARD_ACCOUNT_ID = acc.CCARD_ACCOUNT_ID
  WHERE CCARD_PURCH_OP_STATUS IN ('normal','pending','processed','partially_confirmed')
    AND CCARD_PURCH_OP_TYPE not in ('unknown')
  QUALIFY row_number() over(partition by ccard_prop_id order by CCARD_PURCH_OP_DT asc) =1
),

mar_aberto_tc AS (
  SELECT cus_cust_id_borrower, cast(LAST_UPDATE as date) last_update, flow_resolution, status
  FROM `meli-bi-data.WHOWNER.BT_CRD_OM_APPLICATIONS` t2
  WHERE t2.sit_site_id = 'MLB' and t2.flow_id like '%card%' and t2.status = 'approved'
    and cast(LAST_UPDATE as date) >= '2024-08-01'
),

melimas AS (
  SELECT ORGANIC_FLAG, cus_cust_id,
    tim_day as lyl_from,
    coalesce(lag(tim_day,1) over(partition by cus_cust_id order by tim_day desc),'2999-01-01') as lyl_to
  FROM `meli-bi-data.WHOWNER.DM_LYL_USERS_FLAGS`
  WHERE MELIMAS_FLAG='MELIMAS' AND SIT_SITE_ID IN  ('MLB')
    and TIME_FRAME  = 'WEEK' and tim_day >= '2024-01-01'
),

-- EAs detectados via BT_CCARD_MONZA_AUTO_ISSUANCE_MLB (auto-issuance)
-- Substitui a dependencia da CONTROLE_ENCENDIDOS_CCARD_MLB pra identificar EAs.
-- Fonte: query 0_AUT_TBL_CONGRATS_ADQ_MLB_TOTAL
monza_ea AS (
  SELECT DISTINCT acc.CCARD_PROP_ID
  FROM `meli-bi-data.WHOWNER.BT_CCARD_MONZA_AUTO_ISSUANCE_MLB` ea
  LEFT JOIN `meli-bi-data.WHOWNER.BT_CCARD_ACCOUNT` acc
    ON acc.SIT_SITE_ID = 'MLB'
   AND acc.CCARD_ACCOUNT_ID = ea.CCARD_ACCOUNT_ID
  WHERE ea.CCARD_AI_FLAG_AUTO_ISSUED = TRUE
),

variante AS (
  SELECT CUS_CUST_ID,
    STRING_AGG(distinct status_variante,'.' ORDER BY status_variante asc ) AS status_variante,
    string_agg(distinct status_banco,'.' order by status_banco asc) as status_banco,
    SUM(QTD) AS QTD
  FROM (
    SELECT DISTINCT cus_cust_id,
      case
        when lower(CATEGORY_CARD) like '%infinite%' or lower(category_card) like '%black%' then 'BLACK+'
        when lower(CATEGORY_CARD) like '%platinum%'  then 'PLATINUM'
        when lower(CATEGORY_CARD) like '%signature%' then 'OUTROS'
        when lower(category_card) like '%gold%'  then 'GOLD'
        when lower(category_card) like '%standard%' or lower(category_card) like '%basic%' or lower(category_card) like '%classic%' or lower(category_card) like '%credit%' then 'BASIC'
        when lower(category_card) like '%corp%' or lower(category_card) like '%business%' or lower(category_card) like '%empresarial%' then 'OUTROS'
        when category_card is not null then 'OUTROS'
        WHEN CATEGORY_CARD is null then 'OUTROS'
      end as status_variante,
      CATEGORY_CARD,
      case
        when lower(BANK_MAP) like '%santander%' or lower(BANK_MAP) like '%itau%' or lower(BANK_MAP) like '%bradesco%' or lower(BANK_MAP) like '%banco%do%brasil%' or lower(BANK_MAP) like 'caixa' or lower(BANK_MAP) like '%caixa%economica%federal' or lower(BANK_MAP) like '%safra%'
          then 'Grandes Bancos'
        when lower(BANK_MAP) like '%nubank%' then  'Nubank'
        when lower(BANK_MAP) like '%btg%'or lower(BANK_MAP) like '%banco%xp%' then 'Bancos de Investimento'
        when lower(BANK_MAP) like '%pagseguro%' or lower(BANK_MAP) like '%picpay%' or lower(BANK_MAP) like '%original%' or lower(BANK_MAP) like '%banco%inter%' or lower(BANK_MAP) like '%banco%c6%' then  'Outros Bancos Digitais'
        else 'Outros'
      end as status_banco,
      count(distinct GTW_CARD_ID) as qtd
    FROM `meli-bi-data.WHOWNER.LK_GTW_CARD`  t1
    LEFT JOIN `meli-bi-data.WHOWNER.LK_FRD_BINES` t2
      ON substr(t1.GTW_CR_TRUNC_CARD_NUMBER,1,6) = cast(t2.bin as string)
    WHERE t1.gtw_cr_payment_type_id = 'credit_card'
      and t1.gtw_cr_status ='active'
      and upper(t2.bank_map) not like '%MERCADO%PAGO'
      and sit_site_id = 'MLB'
      AND BANK_MAP not LIKE 'MERCAD%PAGO'
    GROUP BY all
  )
  GROUP BY all
)


SELECT
  prop.DT_ENCENDIDO,
  prop.DT_CONV,
  prop.DIAS_CONV,
  prop.FLAG_TC,
  prop.FLAG_CONVERSAO,
  CASE WHEN prop.NUMERO_PROP <> 1 THEN '2. Reencendido' ELSE '1. Primeiro Encendido' END AS FLAG_REENCENDIDO,
  FLAG_APP_ATIVO,
  APP_INST,

  CASE
    WHEN NUMERO_PROP = 1 THEN '1'
    when numero_prop = 2 then '2'
    when numero_prop = 3 then '3'
    when numero_prop <=5 then '3-5'
    when numero_prop <= 10 then '5-10'
    when numero_prop> 10 then '10+'
  end as range_numero_propostas,

  case
    when coalesce(nise_tag,'BRONZE') in ('BRONZE','SILVER') and CCARD_GLOBAL_LIMIT_AMT_LC>= 0.9*15000 THEN 'Limite Topado'
    when coalesce(nise_tag,'BRONZE') in ('GOLD','PLATINUM') and CCARD_GLOBAL_LIMIT_AMT_LC>= 0.9*20000 then 'Limite Topado'
  end status_limite_topado,

  CASE
    WHEN prop.DT_ENCENDIDO < CAST('2021-07-13' AS DATE) THEN '1. Debit First'
    WHEN DT_CONV = fis.CREATED_DATE THEN '2. Virtual - Credit First'
    WHEN DT_CONV < fis.CREATED_DATE THEN '2. Virtual - Credit First'
    WHEN fis.CUS_CUST_ID IS NULL THEN '2. Virtual - Credit First'
    ELSE '1. Debit First'
  END AS FLAG_PERFIL_USUARIO,

  CASE
    WHEN UPPER(NISE_TAG) = 'BRONZE' THEN '1. Bronze'
    WHEN UPPER(NISE_TAG) = 'SILVER' THEN '2. Silver'
    WHEN UPPER(NISE_TAG) = 'GOLD' THEN '3. Gold'
    WHEN UPPER(NISE_TAG) = 'PLATINUM' THEN '4. Platinum'
    WHEN UPPER(NISE_TAG) IS NULL THEN '5. Nulo'
    ELSE 'CHECAR'
  END AS FLAG_NISE,

  CASE
    WHEN WL.CUS_CUST_ID IS NOT NULL AND WL.DT_WAITLIST < '2023-02-24' THEN "2. Lista de Espera"
    WHEN WL.CUS_CUST_ID IS NOT NULL AND WL.DT_WAITLIST >= '2023-02-24' THEN "3. Mar Aberto OFF"
    ELSE "1. Encendido BAU"
  END AS FLAG_STATUS_WL,

  CASE
    when cc.CRD_CREDIT_CREATION_DATE_ID is null then '4. Sem Uso de CC antes da contratacao TC'
    when ABS(date_diff(CRD_CREDIT_CREATION_DATE_ID, prop.DT_ENCENDIDO,DAY)) <=30 then '1. Ativos Ate 30'
    when ABS(date_diff(CRD_CREDIT_CREATION_DATE_ID, prop.DT_ENCENDIDO,DAY)) <=60 then '2. Ativos Ate 60'
    when ABS(date_diff(CRD_CREDIT_CREATION_DATE_ID, prop.DT_ENCENDIDO,DAY)) <=90 then '3. Ativos Ate 90'
    when ABS(date_diff(CRD_CREDIT_CREATION_DATE_ID, prop.DT_ENCENDIDO,DAY)) <=180 then '5. Ativos Ate 180'
    when ABS(date_diff(CRD_CREDIT_CREATION_DATE_ID, prop.DT_ENCENDIDO,DAY)) <=360 then '6. Ativos Ate 360'
    when ABS(date_diff(CRD_CREDIT_CREATION_DATE_ID, prop.DT_ENCENDIDO,DAY)) > 360 then '7. Ativos > 360'
  END AS FLAG_USO_CC_ANT_ENC_TC,

  case
    when inc<2000 then 'BRONZE'
    when inc<4000 then 'SILVER'
    when inc<10000 then 'GOLD'
    when inc>= 10000 then 'PLATINUM'
  END as flag_nise_ajustada,

  -- SCR removido: dims dependentes setadas como NULL
  CAST(NULL AS STRING) as alcance_limite_tc_vs_limite_scr,
  CAST(NULL AS STRING) as determinante_atendimento,
  CAST(NULL AS STRING) as faixa_atendimento,
  CAST(NULL AS STRING) as faixa_atendimento_anterior,
  CAST(NULL AS STRING) as faixa_uso_tc_fora,
  CAST(NULL AS STRING) as faixa_limite_total,

  case
    when CCARD_GLOBAL_LIMIT_AMT_LC<1000 then '<1000'
    when CCARD_GLOBAL_LIMIT_AMT_LC<2000 then '<2000'
    when CCARD_GLOBAL_LIMIT_AMT_LC<3000 then '<3000'
    when CCARD_GLOBAL_LIMIT_AMT_LC<5000 then '<5000'
    when CCARD_GLOBAL_LIMIT_AMT_LC<6000 then '<6000'
    when CCARD_GLOBAL_LIMIT_AMT_LC<7000 then '<7000'
    when CCARD_GLOBAL_LIMIT_AMT_LC<8000 then '<8000'
    when CCARD_GLOBAL_LIMIT_AMT_LC<10000 then '<10000'
    when CCARD_GLOBAL_LIMIT_AMT_LC< 15000 then '<15000'
    when CCARD_GLOBAL_LIMIT_AMT_LC< 20000 then '<20000'
    when CCARD_GLOBAL_LIMIT_AMT_LC<30000 then '<30000'
    when CCARD_GLOBAL_LIMIT_AMT_LC<50000 then '<50000'
    when CCARD_GLOBAL_LIMIT_AMT_LC>=50000 then '>=50000'
  end as faixa_limite_tcmp,

  bureaus.range_bureau,
  FLAG_SELLERS,
  rating_tc,
  CAST(NULL AS INT64) as status_scr,  -- SCR removido

  case
    when data_ultimo_encendido is null then 'Primeiro Encendido'
    when date_trunc(data_ultimo_apagado, MONTH) = date_trunc(DT_ENCENDIDO, MONTH) then 'Reecendidos - Sem Descanso'
    when date_trunc(data_ultimo_apagado, MONTH) <>  date_trunc(DT_ENCENDIDO, MONTH) then 'Reecendidos - Com Descanso'
  end as status_descanso_reencendidos,
  status_limite_reenc,
  decil as decil_conversao,

  case
    -- EA tem prioridade absoluta: vem de BT_CCARD_MONZA_AUTO_ISSUANCE_MLB.
    -- Substitui a dependencia da CONTROLE_ENCENDIDOS_CCARD_MLB (que tem lag).
    when monza_ea.CCARD_PROP_ID is not null then 'EA - MP'
    when matc.cus_cust_id_borrower is not null AND flow_resolution like '%on_hold%' then 'Mar Aberto Async'
    when matc.cus_cust_id_borrower is not null and  flow_resolution like '%opf%' then 'Mar Aberto Async - OPF Fora'
    when matc.cus_cust_id_borrower is not null then 'Mar Aberto RTS'
    when cenc.aud_ins_user = 'vinicius.girao@mercadopago.com.br' or cenc.aud_ins_user = 'OPF' then 'Encendido OPF'
    WHEN cenc.aud_ins_user in ('guilherme.lafont@mercadopago.com.br','micaela.jauregui@mercadolibre.com','juan.perrota@mercadolibre.com') then policy_description||' Seller'
    when POLICY_DESCRIPTION is not null and date_trunc(cenc.fecha_encendido, month) <'2025-01-01' then policy_description||'-'||CAMPAIGN_GROUP_DESC
    when POLICY_DESCRIPTION is not null and date_trunc(cenc.fecha_encendido, month) >='2025-01-01' then policy_description||'-'||CAMPAIGN_GROUP_DESC
    else 'Outros'
  end as grupo_especial,

  aud_ins_user as referente_riscos,

  case
    when hu.meses_compra_ml >= 5 and tpv_medio_por_mes >= 500 then 'thick com tpv >=500'
    when hu.meses_compra_ml >= 5 and tpv_medio_por_mes < 500 then 'thick com tpv <500'
    else 'thin' end as flag_heavy_user,

  case when upper(CAMPAIGN_GROUP) like '%CONTROL%' then 'Controle' else null end status_controle,

  status_uso_ccard,

  case
    when date_diff( data_uso_ccard, dt_conv, DAY) <=0 then 'Same Day'
    when date_diff( data_uso_ccard, dt_conv, DAY) <=10 then '10D'
    when date_diff( data_uso_ccard, dt_conv, DAY) <=30 then '30D'
    when date_diff( data_uso_ccard, dt_conv, DAY) <=60 then '60D'
    when date_diff( data_uso_ccard, dt_conv, DAY) >60 then '60D+'
    when data_uso_ccard is null then 'Sem Uso'
  end as timing_uso_ccard,

  null as range_investimento,
  null as aum_group,
  case when mas.cus_cust_id is null then 'NOT MELIMAS' else 'MELIMAS' end status_melimas,
  case when mas.cus_cust_id is not null and date_diff(date(ccard_prop_creation_dt),mas.lyl_from,day) <= 10 then 'MELIMAS' else 'NOT MELIMAS' end status_melimas_new,
  null AS ORGANIC_FLAG,
  PLACEMENT,
  bu as flag_canal_aquisicao_simp,
  case when onboarding.cus_cust_id is null then 'Sem Entrada' else 'Com Entrada' end as status_onboarding,
  transac.anomes_ref as dt_ult_transaction,
  app_transacao,

  -- SCR removido: range_qtd_instit sempre '0'
  '0' as range_qtd_instit,

  case
    when status_variante like '%BLACK%' then '1. Black'
    when status_variante like '%PLATINUM%' then '2. Platinum'
    when status_variante like '%GOLD%' then '3. Gold'
    when status_variante like '%BASIC%' then '4. Basic'
    when status_variante like '%OUTROS%' then '5. Outros'
    when status_variante is null   then '6.Sem TC Cadastrada'
  end as flag_variante,
  status_banco as flag_banco_emissor,
  prop.ccard_prop_status,
  case when prop.ccard_prop_status = 'cancelled' and date_trunc(DT_CONV,month) = date_trunc(DT_ENCENDIDO,month) then true else false end status_cancelada_no_mes_de_encendido,
  status_cancelada_anteriormente,

  COUNT(DISTINCT prop.CUS_CUST_ID) AS QTDE,
  sum(CCARD_GLOBAL_LIMIT_AMT_LC) as soma_limite,
  sum(limite_anterior) as soma_limite_anterior,
  sum(inc) as soma_rnd,
  -- SCR removido: agregacoes setadas como NULL
  CAST(NULL AS FLOAT64) as limite_scr,
  CAST(NULL AS FLOAT64) as saldo_utilizado,
  CAST(NULL AS FLOAT64) as max_saldo_r,
  CAST(NULL AS INT64) as qtd_instit,
  CAST(NULL AS FLOAT64) as limite_total,
  sum(NUMERO_PROP) as soma_numero_propostas

FROM proposal as prop

-- JOINs kyc + scr REMOVIDOS (eram a unica fonte de REG_IDENTIFICATION_NUMBER)

LEFT JOIN `meli-bi-data.SBOX_CREDITSTC.SCORE_PROPOSTAS_CCARD` bureaus
  ON bureaus.ccard_prop_id = prop.ccard_prop_id

LEFT JOIN tc_fisica AS fis
  ON prop.CUS_CUST_ID = fis.CUS_CUST_ID
  AND prop.dt_encendido > fis.CREATED_DATE

LEFT JOIN `meli-bi-data.SBOX_CREDITSTC.TBL_ENT_FLUXO_WAITLIST` AS wl
  ON prop.CUS_CUST_ID = wl.CUS_CUST_ID
  AND prop.DT_ENCENDIDO >= DT_WAITLIST

LEFT JOIN uso_cc cc
  ON prop.cus_cust_id = cc.CUS_CUST_ID
  AND CAST(prop.DT_ENCENDIDO AS DATE) >= CRD_CREDIT_CREATION_DATE_ID
  AND prox_uso_cc > CAST(prop.DT_ENCENDIDO AS DATE)
  AND prox_uso_cc <> CRD_CREDIT_CREATION_DATE_ID

LEFT JOIN prop_cc
  ON prop.CUS_CUST_ID = prop_cc.CUS_CUST_ID

LEFT JOIN (
  SELECT * FROM `meli-bi-data.SBOX_CREDITSTC.CONTROLE_ENCENDIDOS_CCARD_MLB`
  QUALIFY row_number() over(partition by cus_cust_id, date_trunc(fecha_encendido,month) order by fecha_encendido desc) = 1
) AS cenc
  ON cenc.cus_cust_id = prop.cus_cust_id
  AND format_date('%Y%m',DT_ENCENDIDO) = anomes_encendido_riscos

LEFT JOIN decil_convs dc
  ON prop.cus_cust_id = dc.cus_cust_id
  AND prop.DT_ENCENDIDO >=fecha_decil_inicio
  AND prop.DT_ENCENDIDO < fecha_decil_fim
  AND fecha_decil_inicio >= prop.DT_ENCENDIDO -35

LEFT JOIN `meli-bi-data.SBOX_CREDITSTC.base_custs_compra_ml_tt` hu
  ON hu.cus_cust_id = prop.cus_cust_id
  AND format_date('%Y%m',DT_ENCENDIDO) = format_date('%Y%m',hu.safra)

LEFT JOIN uso_ccard uso
  ON uso.cus_cust_id = prop.cus_cust_id
  AND uso.ccard_prop_id = prop.ccard_prop_id

LEFT JOIN mar_aberto_tc matc
  ON matc.cus_cust_id_borrower = prop.cus_cust_id
  AND prop.dt_encendido between matc.last_update and matc.last_update+2

LEFT JOIN melimas mas
  ON mas.cus_cust_id = prop.cus_cust_id
  AND dt_encendido >= mas.lyl_from
  AND dt_encendido < mas.lyl_to

LEFT JOIN `meli-bi-data.SBOX_CREDITSTC.0_AUT_TBL_CONGRATS_ADQ_MLB_TOTAL_AJUSTADA` congrats
  ON prop.cus_cust_id = congrats.cus_cust_id
  AND date_trunc(DT_CONV, month) = date_trunc(congrats.DT_aceite,month)
  AND FLAG_CONVERSAO= '1. Convertido'

LEFT JOIN variante
  ON prop.cus_cust_id = variante.cus_cust_id

LEFT JOIN meli-bi-data.SBOX_CREDITSTC.CUSTS_START_ONBOARDING_TC_POR_PROPOSTA onboarding
  ON prop.ccard_prop_id = onboarding.ccard_prop_id

LEFT JOIN app_instalado as app2
  ON app2.cus_cust_id = prop.cus_cust_id

LEFT JOIN `meli-bi-data.SBOX_CREDITSTC.base_users_transacionais_12M` transac
  ON transac.cus_cust_id = prop.cus_cust_id
  AND date_trunc(dt_encendido,month) = '2025-02-01'

LEFT JOIN monza_ea
  ON monza_ea.CCARD_PROP_ID = prop.ccard_prop_id

WHERE CAST(prop.DT_ENCENDIDO AS DATE) >= DATE_SUB(CURRENT_DATE(), INTERVAL 6 MONTH)

GROUP BY all
