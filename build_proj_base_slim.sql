-- ============================================================
-- build_proj_base_slim.sql
-- Tabela intermediaria ENXUTA para a projecao (dashboard Grid).
-- Fonte: base_projecao_Gui (base do usuario, mesma fonte da do Igor).
--
-- Por que existe: a query de projecao re-escaneia a base ~9x (~34 GB/run).
-- Esta tabela recorta ~4 meses + so colunas usadas + dims ja calculadas,
-- deixando os scans baratos e a query da projecao mais simples de evoluir.
--
-- Recriada (CREATE OR REPLACE) pelo pipeline ANTES de rodar a projecao.
--
-- Dims pre-calculadas aqui (uma vez so):
--   super_grupo_base : Sellers/Mar Aberto/Only Nav/Cuentas Canceladas/BAU
--                      (SEM EA -- EA e tratado via flag_ea na query)
--   nise_seller      : '0. SELLER' -> 'SELLER', senao FLAG_NISE
--   flag_ea          : TRUE quando canal = 'EA - MP'
--
-- DECISOES (alinhadas com as abas Emissoes/Encendidos do Grid):
--   Sellers = FLAG_NISE = '0. SELLER'  (base nova nao tem FLAG_SELLERS)
--   EA      = FLAG_CANAL_AQUISICAO = 'EA - MP'
--             (apos o fix do join de placement em 2026-06-21, FLAG_CANAL_AQUISICAO
--              e placement='EA - MP' sao identicos -- borda zero; usar o canal e
--              equivalente a usar placement e reproduz o EA do Igor)
-- ============================================================
CREATE OR REPLACE TABLE `meli-bi-data.SBOX_CREDITSTC.proj_base_slim_gui` AS
SELECT
  DT_ENCENDIDO,
  DT_CONV,
  FLAG_TC,
  FLAG_REENCENDIDO,
  FLAG_NISE,
  FLAG_CANAL_AQUISICAO,
  grupo_especial,
  FLAG_CONVERSAO,
  DIAS_CONV,
  QTDE,
  -- super_grupo base (sem EA)
  CASE
    WHEN FLAG_NISE = '0. SELLER'                              THEN 'Sellers'
    WHEN grupo_especial LIKE '%Mar Aberto%'                   THEN 'Mar Aberto'
    WHEN grupo_especial = 'TEST REACH-TEST NO ECOSISTEMATICOS' THEN 'Only Nav'
    WHEN grupo_especial LIKE '%CANCELADAS%'                   THEN 'Cuentas Canceladas'
    ELSE 'BAU'
  END AS super_grupo_base,
  -- nise com seller colapsado
  CASE WHEN FLAG_NISE = '0. SELLER' THEN 'SELLER' ELSE FLAG_NISE END AS nise_seller,
  -- flag EA (canal)
  (FLAG_CANAL_AQUISICAO = 'EA - MP') AS flag_ea
FROM `meli-bi-data.SBOX_CREDITSTC.base_projecao_Gui`
-- DT_ENCENDIDO: 4m cobre as curvas de hazard (hist usa 3m). DT_CONV: 5m para a
-- aba Projecao mostrar realizado diario de 6 safras (vigente + 5 fechadas).
WHERE DT_ENCENDIDO >= DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL 4 MONTH), MONTH)
   OR DT_CONV      >= DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL 5 MONTH), MONTH)
