-- ============================================================================================
-- RBA_TESTE_AB_MENSAL — marcacao de teste/controle (piso minimo C1/C2) por SAFRA DE ENCENDIDO
-- ============================================================================================
-- POR QUE ISSO EXISTE
--   O time de risco entrega, a cada mes, uma tabela nova:
--     SBOX_CREDITS_SB.RBA_TC_MLB_ENCENDIDO_<MES>_26_TOTAL_ANALISIS_RESUMEN
--   Cada tabela vale SO para a safra dela. A query principal (base_projecao_Gui) fazia
--   left join direto na tabela de JULHO, so por CUS_CUST_ID e SEM recorte de mes -> encendidos
--   de agosto herdavam a marcacao de julho. Medido em 26/08/2026: dos 7.229.720 clientes
--   presentes nas duas tabelas, 190.334 tinham FL_TEST_AB_C1_C2 DIFERENTE (e GRUPO divergia em
--   3,2M, ~44%). Esta view resolve expondo a coluna `safra`, que entra no ON do join.
--
-- COMO ADICIONAR UM MES NOVO  (unica manutencao mensal)
--   1. Copie o ultimo bloco SELECT ... UNION ALL
--   2. Troque o nome da tabela e a data da `safra` (sempre o 1o dia do mes de encendido)
--   3. Rode este CREATE OR REPLACE VIEW. A query principal NAO muda.
--   Se a tabela do mes novo tiver colunas a mais, ignore-as aqui ou adicione na lista das
--   outras safras como CAST(NULL AS <tipo>) — o UNION ALL exige mesma assinatura.
--
-- ⚠️ O JOIN NA QUERY PRINCIPAL **PRECISA** INCLUIR A SAFRA:
--     left join `meli-bi-data.SBOX_CREDITSTC.RBA_TESTE_AB_MENSAL` teste_piso
--            ON teste_piso.CUS_CUST_ID = prop.CUS_CUST_ID
--           AND teste_piso.safra       = DATE_TRUNC(prop.DT_ENCENDIDO, MONTH)
--   Sem o `AND ... safra`, os 7,2M clientes que aparecem em mais de uma safra viram varias
--   linhas cada e a base DUPLICA (fan-out). Cada tabela mensal e unica por CUS_CUST_ID, e a
--   safra e o que mantem o join 1:1.
--
-- NOTA: safras SEM tabela de teste (ex.: antes de jul/26) ficam com FL_TEST_AB_C1_C2 = NULL.
--   Isso e o correto — antes elas pegavam o flag de julho, que nao valia para elas.
-- ============================================================================================
CREATE OR REPLACE VIEW `meli-bi-data.SBOX_CREDITSTC.RBA_TESTE_AB_MENSAL` AS

SELECT DATE '2026-07-01' AS safra,
       CUS_CUST_ID, MODELO, GRUPO, FL_TEST_AB_C1_C2,
       CAST(NULL AS STRING) AS rating_rba          -- julho/26 nao tem RATING na origem
FROM `meli-bi-data.SBOX_CREDITS_SB.RBA_TC_MLB_ENCENDIDO_JULIO_26_TOTAL_ANALISIS_RESUMEN`

UNION ALL

SELECT DATE '2026-08-01' AS safra,
       CUS_CUST_ID, MODELO, GRUPO, FL_TEST_AB_C1_C2,
       RATING AS rating_rba
FROM `meli-bi-data.SBOX_CREDITS_SB.RBA_TC_MLB_ENCENDIDO_AGOSTO_26_TOTAL_ANALISIS_RESUMEN`

-- UNION ALL
-- SELECT DATE '2026-09-01' AS safra,
--        CUS_CUST_ID, MODELO, GRUPO, FL_TEST_AB_C1_C2,
--        RATING AS rating_rba
-- FROM `meli-bi-data.SBOX_CREDITS_SB.RBA_TC_MLB_ENCENDIDO_SETEMBRO_26_TOTAL_ANALISIS_RESUMEN`
;
