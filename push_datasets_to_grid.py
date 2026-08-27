"""
push_datasets_to_grid.py
========================
Atualiza os datasets dos dashboards Grid (arquitetura multi-doc, Central TCMP):

  -> doc Emissoes+Encendidos (DOC_ID = 01KVDVV9XG2ZJ53YEEQRT1E704):
       - mensal_encendidos (safra_enc + dims, encendidos+conv)
       - mensal_emissoes (safra_conv + dims, convertidos apenas)
       - diario (dia_enc + FLAG_TC + FLAG_REENCENDIDO)
       - nprop_enc / nprop_emi (pizza Nº Propostas; só safra+FLAG_TC+range_numero_propostas)

  -> doc Projecao TCMP (PROJ_DOC_ID = 01KVWX9C5JAFHN4QCKP4HZCKB5):
       - projecao (bundle aninhado, 1 linha JSON-string; recria proj_base_slim_gui +
         roda build_projecao_local.py antes do PUT)

Fluxo:
  1. Roda query no BigQuery contra base_projecao_Gui
  2. Converte colunas numericas (BQ JSON exporta como string)
  3. Faz PUT no Grid via /api/v1/documents/{doc}/data/{name}

Agendamento (Windows Task Scheduler):
  push_datasets_to_grid.bat roda 08h00 e 22h00 (1h depois da query
  do Data Suite que atualiza a base_projecao_Gui).

Manutencao:
  - Janela: 13 meses rolling (DATE_SUB ... INTERVAL 12 MONTH)
  - Para mudar dimensoes/filtros, ajustar QUERIES abaixo
  - Tamanho atual: ~36 MB total nos 3 datasets
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# Forcar UTF-8 no stdout (Windows cp1252 nao aceita emoji)
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Resolver bq.cmd / curl.exe explicitamente (subprocess no Windows nao resolve sozinho)
BQ_CMD = shutil.which("bq") or shutil.which("bq.cmd") or "bq.cmd"
CURL_CMD = shutil.which("curl") or shutil.which("curl.exe") or "curl.exe"

DOC_ID = "01KVDVV9XG2ZJ53YEEQRT1E704"        # doc Emissoes + Encendidos (5 datasets)
PROJ_DOC_ID = "01KVWX9C5JAFHN4QCKP4HZCKB5"   # doc Projecao TCMP (so o dataset 'projecao')
LIM_DOC_ID  = "01KYRJTVSJEFPBZV41XY0X36H4"   # doc Limite TCMP (datasets limite_enc, limite_conv)
# Projeto de EXECUCAO/billing dos jobs BQ. Usar o furyid do usuario (nao o meli-bi-data
# compartilhado) p/ evitar "max jobs queued per project" na fila lotada do projeto comum.
# Os jobs leem meli-bi-data.* cross-project normalmente.
# 2026-08-27: billing trocado de ...725... para ...341... O ...725... voltou a travar por quota:
# jobs entram e ficam em PENDING indefinidamente (nao falham, so nao saem da fila). Foi o que
# derrubou a tarefa agendada de 09:30 (rodou 14 min, log ficou so com o cabecalho) e travou o push
# manual em 20 min com 2 jobs empilhados. O ...341... e o padrao ja documentado como estavel.
PROJECT_ID = "ddme000341-ox7qb27ldi8-furyid"
GRID_HOST = "https://grid.melioffice.com"
TMP_DIR = Path(r"C:\Users\GPEREIRADOSS\grid_tmp")
TMP_DIR.mkdir(exist_ok=True)

# Acima deste tamanho, sobe pelo fluxo v2 de URL assinada (upload-url -> PUT -> publish),
# que vai direto pro storage e evita 503/timeout do PUT simples do proxy /data/{name}.
# Abaixo, usa o PUT legado /data/{name} (testado e estavel p/ datasets pequenos).
SIGNED_THRESHOLD_MB = 20
# Limite do PUT legado /data/{name} no Grid (a propria API responde 413 acima disso).
# Usado p/ nao tentar um fallback que ja se sabe que nao cabe (ver upload_dataset).
LEGADO_MAX_MB = 10

# Datasets que sobem em json_columnar (coluna 1x + dados em arrays) p/ economizar ~62%.
# Os demais continuam json_rows. O HTML (fromColumnar) aceita os dois formatos.
COLUMNAR = {"mensal_encendidos", "mensal_emissoes", "nprop_enc", "nprop_emi"}

# Caso de "string -> numero" depois de bq query --format=prettyjson
NUM_COLS = {"n_enc", "n_primo", "n_reenc", "n_conv", "soma_limite", "soma_maxsaldo"}

# ════════════════════════════════════════════════════════════════════════════════════════
# SUPER GRUPO — definicao UNICA (2026-08-27, pedido do usuario)
#
# Antes esta CASE estava COPIADA em 6 queries; qualquer mudanca tinha 6 chances de divergir.
# Agora e uma constante interpolada em todas (inclusive nas de limite, que passaram a ter
# super_grupo).
#
# Grupos novos: BAU · Sellers SMB · Sellers LT · Only Nav. · Teste · Cuentas Canceladas
#   - saiu "Mar Aberto" -> absorvido em BAU (decisao do usuario 27/08)
#   - entrou "Teste": marcacao de teste vigente. Hoje = teste piso minimo C1/C2
#     (FL_TEST_AB_C1_C2 = 1, via view RBA_TESTE_AB_MENSAL). Quando entrar outro teste, e
#     aqui que se troca a condicao.
#
# PRECEDENCIA (decisao do usuario): Teste ganha de TODOS. Assim o grupo "Teste" fecha
# exatamente com o tamanho do teste (583.676 QTDE em ago/26) e da p/ isolar o efeito.
# Depois: Sellers > Only Nav > Cuentas Canceladas > BAU.
#
# UNIVERSO DE SELLER: `PURO_SELLERS = 'SELLER'` (= CUST_TYPE_PYL), conforme o print do
# usuario (27/08). NAO e FLAG_NISE = '0. SELLER', que era o usado antes. Validado
# reproduzindo o print: jul/26 + TC Full + PURO_SELLERS='SELLER' da 252.144 QTDE, exatamente
# o "Total geral" do Tableau dele, grupo a grupo.
#
# ⚠️ SMB x LT — PENDENTE DE UMA COLUNA NA BASE (nao e ambiguidade, e falta de dado):
# A separacao correta e `SEL_SEGMENT` do SCORE_PROPOSTAS_CCARD, que e limpa:
#     LONGTAIL (LOLO + HILO) | SMB (SMB1/2/3) | BIG SELLERS (LM1/LM2/CORP)
# Mas `SEL_SEGMENT` NAO existe em base_projecao_Gui, e a base e AGREGADA (sem CCARD_PROP_ID),
# entao nao da p/ join aqui. Precisa entrar na query principal — que ja faz join nessa tabela
# (alias `bureaus`), logo e 1 linha: adicionar `bureaus.SEL_SEGMENT`.
# Descartado: raspar 'SMB'/'LT' do texto do grupo_especial — cobre so 33% dos sellers
# (jul/26: LT 27,4% + SMB 5,3%; PJ CHA fica 44,3% sem marca e outros 23,1% tambem).
# Enquanto a coluna nao chegar, seller fica em "Sellers" (grupo unico).
# ════════════════════════════════════════════════════════════════════════════════════════

# <<< UNICO PONTO A TROCAR quando `SEL_SEGMENT` entrar na base >>>
# Trocar por:
#   SELLER_SPLIT_SQL = """CASE
#         WHEN SEL_SEGMENT = 'SMB' THEN 'Sellers SMB'
#         WHEN SEL_SEGMENT = 'LONGTAIL' THEN 'Sellers LT'
#         ELSE 'Sellers Outros' END"""   -- BIG SELLERS = 925 em jul/26, decidir com o usuario
# e acrescentar 'Sellers SMB'/'Sellers LT' em SG_VALUES nos HTMLs.
SELLER_SPLIT_SQL = "'Sellers'"

SUPER_GRUPO_SQL = f"""CASE
    WHEN FL_TEST_AB_C1_C2 = 1 THEN "Teste"
    WHEN PURO_SELLERS = "SELLER" THEN {SELLER_SPLIT_SQL}
    WHEN grupo_especial = "TEST REACH-TEST NO ECOSISTEMATICOS" THEN "Only Nav."
    WHEN grupo_especial LIKE "%CANCELADAS%" OR status_cancelada_anteriormente = TRUE THEN "Cuentas Canceladas"
    ELSE "BAU"
  END"""

QUERIES = {
    "mensal_encendidos": f"""
SELECT
  FORMAT_DATE("%Y-%m", DT_ENCENDIDO) AS safra_enc,
  FLAG_TC, FLAG_REENCENDIDO, FLAG_NISE,
  FLAG_CANAL_AQUISICAO AS canal_aquisicao,
  COALESCE(FLAG_APP_ATIVO, "Sem App") AS FLAG_APP_ATIVO,
  {SUPER_GRUPO_SQL} AS super_grupo,
  CASE
    WHEN rating_v7 = "A1" THEN "A1" WHEN rating_v7 = "A2" THEN "A2" WHEN rating_v7 = "A" THEN "A3"
    WHEN rating_v7 = "B1" THEN "B1" WHEN rating_v7 = "B2" THEN "B2" WHEN rating_v7 IN ("B","B3") THEN "B3"
    WHEN rating_v7 IN ("C","C1","C2","C3") THEN rating_v7
    WHEN rating_v7 IN ("D","E","F","G","J","J1","J2") THEN "D-J"
    WHEN rating_v7 IS NULL OR rating_v7 = "Z" THEN "Sem rating"
    ELSE "Outros" END AS rating_tc_grp,
  SUM(QTDE) AS n_enc,
  SUM(IF(FLAG_REENCENDIDO = "1. Primeiro Encendido", QTDE, 0)) AS n_primo,
  SUM(IF(FLAG_REENCENDIDO = "2. Reencendido", QTDE, 0)) AS n_reenc,
  SUM(IF(FLAG_CONVERSAO = "1. Convertido", QTDE, 0)) AS n_conv,
  SUM(soma_limite) AS soma_limite
FROM `meli-bi-data.SBOX_CREDITSTC.base_projecao_Gui`
WHERE DT_ENCENDIDO >= DATE_SUB(DATE_TRUNC(CURRENT_DATE(), MONTH), INTERVAL 12 MONTH)
GROUP BY ALL
""",
    "mensal_emissoes": f"""
SELECT
  FORMAT_DATE("%Y-%m", DT_CONV) AS safra_conv,
  FLAG_TC, FLAG_REENCENDIDO, FLAG_NISE,
  FLAG_CANAL_AQUISICAO AS canal_aquisicao,
  COALESCE(FLAG_APP_ATIVO, "Sem App") AS FLAG_APP_ATIVO,
  {SUPER_GRUPO_SQL} AS super_grupo,
  CASE
    WHEN rating_v7 = "A1" THEN "A1" WHEN rating_v7 = "A2" THEN "A2" WHEN rating_v7 = "A" THEN "A3"
    WHEN rating_v7 = "B1" THEN "B1" WHEN rating_v7 = "B2" THEN "B2" WHEN rating_v7 IN ("B","B3") THEN "B3"
    WHEN rating_v7 IN ("C","C1","C2","C3") THEN rating_v7
    WHEN rating_v7 IN ("D","E","F","G","J","J1","J2") THEN "D-J"
    WHEN rating_v7 IS NULL OR rating_v7 = "Z" THEN "Sem rating"
    ELSE "Outros" END AS rating_tc_grp,
  SUM(QTDE) AS n_conv,
  SUM(soma_limite) AS soma_limite
FROM `meli-bi-data.SBOX_CREDITSTC.base_projecao_Gui`
WHERE FLAG_CONVERSAO = "1. Convertido"
  AND DT_CONV >= DATE_SUB(DATE_TRUNC(CURRENT_DATE(), MONTH), INTERVAL 12 MONTH)
GROUP BY ALL
""",
    "diario": f"""
SELECT
  CAST(DT_ENCENDIDO AS STRING) AS dia_enc,
  FORMAT_DATE("%Y-%m", DT_ENCENDIDO) AS safra_enc,
  FLAG_TC,
  {SUPER_GRUPO_SQL} AS super_grupo,
  SUM(QTDE) AS n_enc
FROM `meli-bi-data.SBOX_CREDITSTC.base_projecao_Gui`
WHERE DT_ENCENDIDO >= DATE_SUB(DATE_TRUNC(CURRENT_DATE(), MONTH), INTERVAL 12 MONTH)
GROUP BY ALL
ORDER BY dia_enc
""",
    # Datasets Nº Propostas — enriquecidos c/ dims-chave p/ reagir aos filtros do dash
    # (NISE, Rating TC, Super Grupo, Canal, Tipo TC, Safra). NAO carregam Reencendido/Uso CC/
    # App/Cancel (decisao de peso do usuario 28/07). Columnar. Front (filtrar) reage sozinho.
    "nprop_enc": f"""
SELECT
  FORMAT_DATE("%Y-%m", DT_ENCENDIDO) AS safra_enc,
  FLAG_TC, FLAG_NISE,
  FLAG_CANAL_AQUISICAO AS canal_aquisicao,
  {SUPER_GRUPO_SQL} AS super_grupo,
  CASE
    WHEN rating_v7 = "A1" THEN "A1" WHEN rating_v7 = "A2" THEN "A2" WHEN rating_v7 = "A" THEN "A3"
    WHEN rating_v7 = "B1" THEN "B1" WHEN rating_v7 = "B2" THEN "B2" WHEN rating_v7 IN ("B","B3") THEN "B3"
    WHEN rating_v7 IN ("C","C1","C2","C3") THEN rating_v7
    WHEN rating_v7 IN ("D","E","F","G","J","J1","J2") THEN "D-J"
    WHEN rating_v7 IS NULL OR rating_v7 = "Z" THEN "Sem rating"
    ELSE "Outros" END AS rating_tc_grp,
  range_numero_propostas,
  SUM(QTDE) AS n_enc
FROM `meli-bi-data.SBOX_CREDITSTC.base_projecao_Gui`
WHERE DT_ENCENDIDO >= DATE_SUB(DATE_TRUNC(CURRENT_DATE(), MONTH), INTERVAL 12 MONTH)
GROUP BY ALL
""",
    "nprop_emi": f"""
SELECT
  FORMAT_DATE("%Y-%m", DT_CONV) AS safra_conv,
  FLAG_TC, FLAG_NISE,
  FLAG_CANAL_AQUISICAO AS canal_aquisicao,
  {SUPER_GRUPO_SQL} AS super_grupo,
  CASE
    WHEN rating_v7 = "A1" THEN "A1" WHEN rating_v7 = "A2" THEN "A2" WHEN rating_v7 = "A" THEN "A3"
    WHEN rating_v7 = "B1" THEN "B1" WHEN rating_v7 = "B2" THEN "B2" WHEN rating_v7 IN ("B","B3") THEN "B3"
    WHEN rating_v7 IN ("C","C1","C2","C3") THEN rating_v7
    WHEN rating_v7 IN ("D","E","F","G","J","J1","J2") THEN "D-J"
    WHEN rating_v7 IS NULL OR rating_v7 = "Z" THEN "Sem rating"
    ELSE "Outros" END AS rating_tc_grp,
  range_numero_propostas,
  SUM(QTDE) AS n_conv
FROM `meli-bi-data.SBOX_CREDITSTC.base_projecao_Gui`
WHERE FLAG_CONVERSAO = "1. Convertido"
  AND DT_CONV >= DATE_SUB(DATE_TRUNC(CURRENT_DATE(), MONTH), INTERVAL 12 MONTH)
GROUP BY ALL
""",
}

# ── Dataset 'adoption' (aba "% Adoption") ──────────────────────────────────────
# Grain: safra_enc x FLAG_TC x FLAG_NISE x rating_tc_grp x super_grupo x range_numero_propostas.
# Medidas: n_enc (denominador = base de encendido) + cc0..cc40 = cumulativo de convertidos
# por janela de dias (ccK = convertidos com DIAS_CONV <= K). O cliente faz adoption(N) =
# SUM(ccN) / SUM(n_enc) por quebra/filtro, recalculando ao mover o slider (0..40).
_ADOPT_MAXDIAS = 40
_cc_cols = ",\n  ".join(
    f'SUM(IF(FLAG_CONVERSAO = "1. Convertido" AND DIAS_CONV <= {k}, QTDE, 0)) AS cc{k}'
    for k in range(_ADOPT_MAXDIAS + 1)
)
QUERIES["adoption"] = f"""
SELECT
  FORMAT_DATE("%Y-%m", DT_ENCENDIDO) AS safra_enc,
  FLAG_TC, FLAG_NISE,
  {SUPER_GRUPO_SQL} AS super_grupo,
  CASE
    WHEN rating_v7 = "A1" THEN "A1" WHEN rating_v7 = "A2" THEN "A2" WHEN rating_v7 = "A" THEN "A3"
    WHEN rating_v7 = "B1" THEN "B1" WHEN rating_v7 = "B2" THEN "B2" WHEN rating_v7 IN ("B","B3") THEN "B3"
    WHEN rating_v7 IN ("C","C1","C2","C3") THEN rating_v7
    WHEN rating_v7 IN ("D","E","F","G","J","J1","J2") THEN "D-J"
    WHEN rating_v7 IS NULL OR rating_v7 = "Z" THEN "Sem rating"
    ELSE "Outros" END AS rating_tc_grp,
  range_numero_propostas,
  COALESCE(FLAG_APP_ATIVO, "Sem App") AS FLAG_APP_ATIVO,
  (FLAG_CANAL_AQUISICAO = "EA - MP") AS flag_ea,
  SUM(QTDE) AS n_enc,
  {_cc_cols}
FROM `meli-bi-data.SBOX_CREDITSTC.base_projecao_Gui`
WHERE DT_ENCENDIDO >= DATE_SUB(DATE_TRUNC(CURRENT_DATE(), MONTH), INTERVAL 12 MONTH)
GROUP BY ALL
"""
COLUMNAR.add("adoption")  # muitas colunas cc* -> columnar economiza espaco
NUM_COLS.update({"n_enc"} | {f"cc{k}" for k in range(_ADOPT_MAXDIAS + 1)})


def run_bq(sql: str, dest_path: Path) -> None:
    """Roda query no BigQuery e salva resultado em JSON.

    SQL passado via stdin pra evitar problemas com caracteres especiais (%, ", newlines)
    no command-line do Windows.
    """
    cmd = [
        BQ_CMD, "query",
        f"--project_id={PROJECT_ID}",
        "--use_legacy_sql=false",
        "--format=prettyjson",
        "--max_rows=500000",
    ]
    result = subprocess.run(
        cmd, input=sql, capture_output=True, text=True, shell=False, encoding="utf-8"
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"bq query falhou (rc={result.returncode})\nSTDOUT: {result.stdout[:500]}\nSTDERR: {result.stderr[:500]}"
        )
    with open(dest_path, "w", encoding="utf-8") as f:
        f.write(result.stdout)


def to_numbers(rows: list) -> list:
    """BQ JSON exporta numeros como string. Converte de volta."""
    for r in rows:
        for c in NUM_COLS:
            if c in r and r[c] is not None:
                v = r[c]
                r[c] = float(v) if "." in str(v) else int(v)
    return rows


REPO         = Path(__file__).resolve().parent
SLIM_SQL     = REPO / "build_proj_base_slim.sql"
PROJ_BUILDER = REPO / "build_projecao_local.py"
PROJ_BUNDLE  = REPO / "_proj_data.json"


def build_projecao_bundle() -> None:
    """Recria a tabela enxuta + roda o builder local -> _proj_data.json (bundle da aba Projecao)."""
    sql = SLIM_SQL.read_text(encoding="utf-8")
    r = subprocess.run(
        [BQ_CMD, "query", f"--project_id={PROJECT_ID}", "--use_legacy_sql=false", "--format=none"],
        input=sql, capture_output=True, text=True, encoding="utf-8", shell=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"build_proj_base_slim falhou: {r.stderr[:400]}")
    r = subprocess.run(
        [sys.executable, str(PROJ_BUILDER)],
        capture_output=True, text=True, encoding="utf-8", shell=False,
    )
    if r.returncode != 0:
        raise RuntimeError(f"build_projecao_local falhou: {r.stderr[:400]}")


def push_projecao(rebuild: bool = True, doc_id: str = PROJ_DOC_ID) -> dict:
    """Dataset `projecao`: bundle aninhado empacotado como 1 linha JSON-string.
    Front faz JSON.parse(rows[0].bundle). rebuild=False reusa o _proj_data.json existente.
    Vai pro doc PROJ_DOC_ID (satelite Projecao), nao pro doc principal."""
    if rebuild:
        build_projecao_bundle()
    bundle_txt = PROJ_BUNDLE.read_text(encoding="utf-8")
    return put_dataset("projecao", [{"bundle": bundle_txt}], doc_id=doc_id)


def put_dataset(name: str, rows: list, doc_id: str = DOC_ID) -> dict:
    """PUT pro Grid via curl."""
    payload_path = TMP_DIR / f"{name}.json"
    with open(payload_path, "w", encoding="utf-8") as f:
        json.dump({"rows": rows, "meta": {"source": "bigquery"}}, f, separators=(",", ":"))
    size_mb = payload_path.stat().st_size / 1024 / 1024

    url = f"{GRID_HOST}/api/v1/documents/{doc_id}/data/{name}"
    cmd = [
        CURL_CMD, "-s",
        "-w", "\n%{http_code}",
        "-X", "PUT", url,
        "-H", "Content-Type: application/json",
        "--data-binary", f"@{payload_path}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    output = result.stdout.strip().rsplit("\n", 1)
    http_code = output[-1] if len(output) > 1 else "?"
    body = output[0] if len(output) > 1 else result.stdout
    return {
        "size_mb": round(size_mb, 2),
        "http": http_code,
        "ok": http_code == "200",
        "body": body[:400] if http_code != "200" else "",
    }


def to_columnar(rows: list) -> dict:
    """Converte lista de objetos -> {columns:[...], data:{coluna:[valores]}} (json_columnar).
    Economiza ~62% vs json_rows (nao repete o nome da coluna em toda linha)."""
    cols = list(rows[0].keys())
    return {"columns": cols, "data": {c: [r.get(c) for r in rows] for c in cols}}


def _set_definition_format(base: str, fmt: str) -> None:
    """POST idempotente na definicao p/ garantir o format (json_rows | json_columnar)."""
    subprocess.run(
        [CURL_CMD, "-s", "-X", "POST", base, "-H", "Content-Type: application/json",
         "-d", json.dumps({"source_type": "external_push", "refresh_mode": "external",
                           "format": fmt, "source": "bigquery"})],
        capture_output=True, text=True,
    )


def put_dataset_signed(name: str, payload_path: Path, size_mb: float, fmt: str = "json_rows", doc_id: str = DOC_ID) -> dict:
    """Sobe dataset pelo fluxo v2 (URL assinada): set format -> upload-url -> PUT -> publish.

    IMPORTANTE (descoberto no spike):
    - o corpo precisa casar com o format da DEFINICAO:
        json_rows     -> array puro            [{...},{...}]
        json_columnar -> {"columns":[...], "data":{coluna:[valores]}}  (data e OBJETO)
    - p/ arquivos grandes o staged file leva ~segundos pra propagar no storage; o publish
      pode retornar 409 "dataset_staged_file_not_ready" (retryable) -> retry com backoff.
    """
    base = f"{GRID_HOST}/api/v1/documents/{doc_id}/datasets/{name}"

    # 1+2) URL assinada (+ revision) e PUT do conteudo, COM RETRY em revision obsoleta.
    #
    # 2026-08-25: o `adoption` falhava recorrentemente com http=put-409
    # {"error":"dataset_revision_mismatch"} — no PASSO 2 (PUT dos bytes), nao no publish.
    # Causa: o POST da definicao (_set_definition_format) bump a revision do dataset; a
    # revision devolvida pelo /upload-url logo depois as vezes ja nasce obsoleta (a escrita
    # da definicao ainda estava assentando). E corrida, por isso era intermitente — e por isso
    # simplesmente re-rodar o push resolvia. Agora, em revision_mismatch, pede uma revision
    # NOVA e tenta de novo, em vez de desistir e cair num fallback que nao cabe.
    put_code, put_body = "?", ""
    for tentativa in range(4):
        _set_definition_format(base, fmt)
        r = subprocess.run(
            [CURL_CMD, "-s", "-X", "POST", f"{base}/upload-url",
             "-H", "Content-Type: application/json", "-d", "{}"],
            capture_output=True, text=True,
        )
        try:
            info = json.loads(r.stdout)
            rev, upload_url = info["revision"], info["upload_url"]
        except Exception:
            return {"size_mb": round(size_mb, 2), "http": "upload-url-fail", "ok": False, "body": r.stdout[:300]}

        rp = subprocess.run(
            [CURL_CMD, "-s", "-w", "\n%{http_code}", "-X", "PUT", upload_url,
             "-H", "Content-Type: application/json", "--data-binary", f"@{payload_path}"],
            capture_output=True, text=True,
        )
        put_code = rp.stdout.strip().rsplit("\n", 1)[-1]
        put_body = rp.stdout[:300]
        if put_code in ("200", "201", "204"):
            break
        if put_code == "409" and "revision_mismatch" in put_body:
            print(f"    [retry {tentativa + 1}/3] revision obsoleta no PUT de '{name}' -> pedindo revision nova")
            time.sleep(2 + 2 * tentativa)
            continue
        # 2026-08-26: transiente de rede/gateway tambem precisa de retry. O `mensal_encendidos`
        # (19 MB, o maior upload) morreu com put-503 "upstream connect error / connection
        # termination" no meio do PUT. Nao e revision, e a conexao caindo — e antes o retry so
        # cobria o 409, entao um 503 derrubava o dataset e o run inteiro (overall=FAIL).
        # 000 = curl nao conseguiu completar a conexao (mesmo caso).
        if put_code in ("000", "500", "502", "503", "504", "408", "429"):
            print(f"    [retry {tentativa + 1}/3] falha transiente (http={put_code}) no PUT de "
                  f"'{name}' -> nova tentativa")
            time.sleep(5 + 5 * tentativa)
            continue
        break  # erro nao-retryable
    if put_code not in ("200", "201", "204"):
        return {"size_mb": round(size_mb, 2), "http": f"put-{put_code}", "ok": False, "body": put_body}

    # 3) publica a revision (com retry p/ propagacao do staged file)
    pub_code, body = "?", ""
    for attempt in range(6):
        rpub = subprocess.run(
            [CURL_CMD, "-s", "-w", "\n%{http_code}", "-X", "POST", f"{base}/publish",
             "-H", "Content-Type: application/json", "-d", json.dumps({"revision": rev})],
            capture_output=True, text=True,
        )
        out = rpub.stdout.strip().rsplit("\n", 1)
        pub_code = out[-1] if len(out) > 1 else "?"
        body = out[0] if len(out) > 1 else rpub.stdout
        if pub_code == "200" and '"published":true' in body.replace(" ", ""):
            return {"size_mb": round(size_mb, 2), "http": "signed-200", "ok": True, "body": ""}
        if pub_code == "409" and '"retry":true' in body.replace(" ", ""):
            time.sleep(3)
            continue
        break  # erro nao-retryable
    return {"size_mb": round(size_mb, 2), "http": f"signed-{pub_code}", "ok": False, "body": body[:300]}


def upload_dataset(name: str, rows: list, doc_id: str = DOC_ID) -> dict:
    """Dispatcher:
    - datasets em COLUMNAR: serializa json_columnar e sobe pelo fluxo assinado v2 (menor + sem 503).
    - demais grandes (>SIGNED_THRESHOLD_MB): fluxo assinado v2 em json_rows (array puro).
    - pequenos: PUT legado /data/{name} (envelope {"rows":..,"meta":..}).
    Em qualquer falha do fluxo assinado, cai no PUT legado (que ainda funciona ate ~42 MB)."""
    if name in COLUMNAR:
        payload_path = TMP_DIR / f"{name}_col.json"
        with open(payload_path, "w", encoding="utf-8") as f:
            json.dump(to_columnar(rows), f, separators=(",", ":"))
        size_mb = payload_path.stat().st_size / 1024 / 1024
        res = put_dataset_signed(name, payload_path, size_mb, fmt="json_columnar", doc_id=doc_id)
        if res["ok"]:
            return res
        # O fallback legado (PUT /data/{name}) tem limite de 10 MB no Grid. Pro `adoption` o
        # json_rows da ~46 MB, entao o fallback SEMPRE morria com http=413 — so trocava um erro
        # por outro, deixava o format do dataset em json_rows (piorando o proximo run) e
        # marcava overall=FAIL. Se nao couber, nao tenta: reporta o erro real do fluxo assinado.
        rows_path = TMP_DIR / f"{name}_array.json"
        with open(rows_path, "w", encoding="utf-8") as f:
            json.dump(rows, f, separators=(",", ":"))
        rows_mb = rows_path.stat().st_size / 1024 / 1024
        if rows_mb > LEGADO_MAX_MB:
            print(f"  [WARN] columnar assinado falhou (http={res['http']}; {res['body'][:120]})")
            print(f"  [SKIP] fallback legado nao cabe: json_rows={rows_mb:.1f} MB > limite {LEGADO_MAX_MB} MB "
                  f"-> mantendo columnar; re-rodar o push resolve se for corrida de revision")
            return res
        print(f"  [WARN] columnar assinado falhou (http={res['http']}; {res['body'][:120]}) -> fallback PUT legado json_rows")
        # fallback: restaura format json_rows e usa o PUT legado
        _set_definition_format(f"{GRID_HOST}/api/v1/documents/{doc_id}/datasets/{name}", "json_rows")
        return put_dataset(name, rows, doc_id=doc_id)

    array_path = TMP_DIR / f"{name}_array.json"
    with open(array_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, separators=(",", ":"))
    size_mb = array_path.stat().st_size / 1024 / 1024
    if size_mb > SIGNED_THRESHOLD_MB:
        res = put_dataset_signed(name, array_path, size_mb, fmt="json_rows", doc_id=doc_id)
        if res["ok"]:
            return res
        print(f"  [WARN] fluxo assinado falhou (http={res['http']}; {res['body'][:120]}) -> fallback PUT legado")
    return put_dataset(name, rows, doc_id=doc_id)


# ── Esconder o mês vigente das abas Emissões/Encendidos/Adoption até o encendido do
#    mês "bater o batch" (senão a safra vigente vem ~vazia e polui/distorce os gráficos).
#    A Projeção NÃO é afetada (projeta o mês vigente normalmente).
BATCH_THRESHOLD = 0.25  # mês vigente entra nas abas v6 quando enc >= 25% do mês anterior
# Override manual: força mostrar ESTE mês vigente nas abas v6 mesmo PRE-BATCH.
# Self-expira na virada de mês (só vale se == mês vigente); em outro mês volta ao batch normal.
# Deixar None quando não precisar forçar.
FORCE_SHOW_MONTH = "2026-07"
EXCL_COL = {  # coluna de data p/ cortar o mês vigente incompleto, por dataset
    "mensal_encendidos": "DT_ENCENDIDO", "diario": "DT_ENCENDIDO",
    "nprop_enc": "DT_ENCENDIDO", "adoption": "DT_ENCENDIDO",
    "mensal_emissoes": "DT_CONV", "nprop_emi": "DT_CONV",
}

def current_month_batched() -> bool:
    """True se o encendido do mês vigente já >= BATCH_THRESHOLD do mês anterior (batch rodou)."""
    sql = """
SELECT
  SUM(IF(DATE_TRUNC(DT_ENCENDIDO,MONTH)=DATE_TRUNC(CURRENT_DATE(),MONTH), QTDE, 0)) AS cur_enc,
  SUM(IF(DATE_TRUNC(DT_ENCENDIDO,MONTH)=DATE_TRUNC(DATE_SUB(CURRENT_DATE(),INTERVAL 1 MONTH),MONTH), QTDE, 0)) AS prev_enc
FROM `meli-bi-data.SBOX_CREDITSTC.base_projecao_Gui`
WHERE DT_ENCENDIDO >= DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL 1 MONTH), MONTH)
"""
    p = TMP_DIR / "_batch_check.json"
    run_bq(sql, p)
    row = json.load(open(p, "r", encoding="utf-8"))[0]
    cur = float(row.get("cur_enc") or 0); prev = float(row.get("prev_enc") or 0)
    batched = prev > 0 and cur >= BATCH_THRESHOLD * prev
    if not batched and FORCE_SHOW_MONTH == time.strftime("%Y-%m"):
        print(f"  [batch-check] enc vigente={int(cur):,} vs anterior={int(prev):,}"
              f" -> PRE-BATCH, mas FORCE_SHOW_MONTH={FORCE_SHOW_MONTH} ativo -> MOSTRA mes vigente".replace(",", "."))
        return True
    print(f"  [batch-check] enc vigente={int(cur):,} vs anterior={int(prev):,}"
          f" -> {'BATCH OK (mostra mes vigente)' if batched else 'PRE-BATCH (esconde mes vigente das abas v6)'}".replace(",", "."))
    return batched


# ============================================================
# Datasets do doc Limite TCMP (limite_enc por safra de encendido; limite_conv por mes de emissao/DT_CONV).
# Janela dinamica: mes YoY (12m atras) + do 1o mes do ano corrente ate o mes vigente.
# rating_v7 (C puro -> C1). Grao inclui FLAG_CONVERSAO/REENCENDIDO/APP p/ os filtros do dashboard.
# ============================================================
_LIM_RAT = """CASE WHEN rating_v7='A1' THEN 'A1' WHEN rating_v7='A2' THEN 'A2' WHEN rating_v7='A' THEN 'A3'
    WHEN rating_v7='B1' THEN 'B1' WHEN rating_v7='B2' THEN 'B2' WHEN rating_v7 IN ('B','B3') THEN 'B3'
    WHEN rating_v7 IN ('C','C1') THEN 'C1' WHEN rating_v7='C2' THEN 'C2' WHEN rating_v7='C3' THEN 'C3'
    WHEN rating_v7 IN ('D','E','F','G','J','J1','J2') THEN 'D-J'
    WHEN rating_v7 IS NULL OR rating_v7='Z' THEN 'Sem rating' ELSE 'Outros' END"""
_LIM_WIN = """({dt} >= DATE_SUB(DATE_TRUNC(CURRENT_DATE(),MONTH), INTERVAL 12 MONTH)
       AND {dt} < DATE_SUB(DATE_TRUNC(CURRENT_DATE(),MONTH), INTERVAL 11 MONTH))
   OR ({dt} >= DATE_TRUNC(CURRENT_DATE(),YEAR)
       AND {dt} < DATE_ADD(DATE_TRUNC(CURRENT_DATE(),MONTH), INTERVAL 1 MONTH))"""
LIM_QUERIES = {
    "limite_enc": f"""
SELECT
  FORMAT_DATE('%Y-%m', DT_ENCENDIDO) AS safra,
  FLAG_TC, FLAG_NISE,
  {_LIM_RAT} AS rating,
  FLAG_CONVERSAO, FLAG_REENCENDIDO,
  {SUPER_GRUPO_SQL} AS super_grupo,
  SUM(QTDE) AS n, ROUND(SUM(soma_limite)) AS soma_limite,
  ROUND(SUM(max_saldo_r)) AS soma_maxsaldo
FROM `meli-bi-data.SBOX_CREDITSTC.base_projecao_Gui`
WHERE {_LIM_WIN.format(dt='DT_ENCENDIDO')}
GROUP BY 1,2,3,4,5,6,7
""",
    "limite_conv": f"""
SELECT
  FORMAT_DATE('%Y-%m', DT_CONV) AS safra,
  FLAG_TC, FLAG_NISE,
  {_LIM_RAT} AS rating,
  FLAG_REENCENDIDO,
  {SUPER_GRUPO_SQL} AS super_grupo,
  SUM(QTDE) AS n, ROUND(SUM(soma_limite)) AS soma_limite,
  ROUND(SUM(max_saldo_r)) AS soma_maxsaldo
FROM `meli-bi-data.SBOX_CREDITSTC.base_projecao_Gui`
WHERE ({_LIM_WIN.format(dt='DT_CONV')})
  AND FLAG_CONVERSAO='1. Convertido'
GROUP BY 1,2,3,4,5,6
""",
}


# ============================================================
# FONTE DO RATING (2026-08-18): usar rating_tc_new onde existe, cair no rating_tc (V6) senao.
#   Historico: 08-05 usava COALESCE(rating_v7, rating_tc) (rating_v7 = DE-PARA so de jul/Full).
#   Agora o usuario pediu a coluna nova `rating_tc_new` (re-rating oficial). Validado 08-18:
#   rating_tc_new so existe de JUN/26 em diante (100% nula antes) -> NAO da p/ usar sozinha
#   (zeraria o rating do historico). Entao: COALESCE(rating_tc_new, rating_tc). Efeito: Jun+ usa
#   o rating novo (ago D-J cai de 11,4% -> ~5,7%, corrige o excesso), historico segue no V6.
#   O CASE de rating em cada query e escrito com o token `rating_v7` -> troco o token pela fonte
#   nova num replace unico (vale p/ mensal_*, nprop_*, adoption, limite_*). rating_v7 aposentado.
#   2026-08-18 (2): usuario apontou que o rating_tc_new de JUNHO esta ruim (D-J 16,9% no TC Full,
#   nao deveria) — backfill de jun mal feito. jul/ago estao bons (D-J 5,7%). Entao rating_tc_new
#   vale so p/ encendidos de JUL/26 em diante; jun e antes usam V6 (rating_tc). Corte por
#   DT_ENCENDIDO (coluna do rating = do encendido; existe em todas as queries da base).
#
# 2026-08-21 — RESOLVIDO NA BASE (base_projecao_Gui v2). O COALESCE acima estava ERRADO por cliente:
#   V6 e V7 rodam EM PARALELO em BT_VU_MODEL_RATING (SCD2 por CRD_MODEL+CRD_VERSION), logo
#   rating_tc_new vem preenchida tambem p/ quem segue encendido no V6 -> o COALESCE sempre pegava
#   tc_new e errava o rating de ~41-49% dos TC Full (acerto de so 18-47% no V6 vs RBA de ago/26).
#   A base v2 passou a expor `rating_efetivo`, que escolhe a coluna certa a partir do MODELO que de
#   fato decidiu o encendido (derivado do grupo_especial: IN IN V6/V7, SWAP IN, CHA V1/V2) e p/
#   seller prioriza o modelo merchant. Acerto vs RBA: 100% nao-seller / ~99% seller. O fallback por
#   data virou desnecessario (sem tag => rating_tc, que cobre todo o historico pre-jul/26).
#   Entao aqui basta trocar o token pela coluna nova. `rating_v7` foi REMOVIDA da base na v2.
def _rating_source(sql: str) -> str:
    return sql.replace("rating_v7", "rating_efetivo")

for _qk in QUERIES:
    if "rating_v7" in QUERIES[_qk]:
        QUERIES[_qk] = _rating_source(QUERIES[_qk])
for _qk in LIM_QUERIES:
    if "rating_v7" in LIM_QUERIES[_qk]:
        LIM_QUERIES[_qk] = _rating_source(LIM_QUERIES[_qk])


def main():
    # Push seletivo (one-off):
    #   python push_datasets_to_grid.py                 -> tudo
    #   python push_datasets_to_grid.py projecao         -> so projecao (rebuild) -> PROJ_DOC_ID
    #   python push_datasets_to_grid.py projecao --no-rebuild  -> so projecao reusando _proj_data.json
    #   python push_datasets_to_grid.py limite           -> so limite_enc + limite_conv
    #   python push_datasets_to_grid.py --only=adoption  -> SO esse dataset (aceita lista:
    #                                                       --only=adoption,diario)
    #
    # 2026-08-27: `--only` criado porque recuperar UM dataset que falhou obrigava re-rodar o
    # push inteiro (9 queries + 9 uploads, ~12 min) — e nesse meio tempo um job do BQ podia
    # entrar em fila e travar tudo de novo. Aconteceu 3x seguidas com o `adoption`.
    args = [a for a in sys.argv[1:]]
    only_proj = "projecao" in args
    only_lim = "limite" in args
    rebuild_proj = "--no-rebuild" not in args
    only_names = set()
    for a in args:
        if a.startswith("--only="):
            only_names |= {x.strip() for x in a.split("=", 1)[1].split(",") if x.strip()}
    if only_names:
        desconhecidos = only_names - set(QUERIES) - set(LIM_QUERIES) - {"projecao"}
        if desconhecidos:
            print(f"  [ERRO] dataset(s) desconhecido(s) em --only: {sorted(desconhecidos)}")
            print(f"         validos: {sorted(set(QUERIES) | set(LIM_QUERIES) | {'projecao'})}")
            return 1

    print(f"[push_datasets] Start {time.strftime('%Y-%m-%d %H:%M:%S')}"
          + (f" (SO projecao, rebuild={rebuild_proj})" if only_proj else ""))
    overall_ok = True

    if not only_proj and not only_lim:
        exclude_cur = not current_month_batched()  # esconde mês vigente das abas v6 até o batch
        for name, sql in QUERIES.items():  # 6 datasets -> doc principal (Emissoes+Encendidos)
            if only_names and name not in only_names:
                continue
            if exclude_cur and name in EXCL_COL:
                # Mês vigente escondido (pre-batch): (1) estende a janela 1 mês a mais p/ trás
                # (12->13 meses) p/ manter o YoY do ÚLTIMO mês exibido — senão o comparativo YoY
                # some cedo demais (ex.: em ago pre-batch o último mês é jul/26 mas jul/25 já
                # teria caído). Quando o mês vigente entra no batch, volta a 12 meses e o mês
                # YoY antigo cai exatamente quando a barra do mês novo aparece.
                # (2) corta o mês vigente (~vazio) das abas v6.
                sql = sql.replace("INTERVAL 12 MONTH", "INTERVAL 13 MONTH", 1)
                sql = sql.replace("GROUP BY ALL",
                                  f"  AND {EXCL_COL[name]} < DATE_TRUNC(CURRENT_DATE(), MONTH)\nGROUP BY ALL", 1)
            print(f"\n--- {name} ---")
            raw_path = TMP_DIR / f"{name}_raw.json"

            t0 = time.time()
            try:
                run_bq(sql, raw_path)
            except Exception as e:
                print(f"  [FAIL] bq query: {e}")
                overall_ok = False
                continue
            t_bq = time.time() - t0

            rows = json.load(open(raw_path, "r", encoding="utf-8"))
            rows = to_numbers(rows)
            print(f"  rows={len(rows):,}  bq_time={t_bq:.1f}s")

            t0 = time.time()
            res = upload_dataset(name, rows)  # default doc_id=DOC_ID
            t_put = time.time() - t0
            status = "[OK]" if res["ok"] else "[FAIL]"
            print(f"  {status} upload http={res['http']}  size={res['size_mb']} MB  up_time={t_put:.1f}s")
            if not res["ok"]:
                print(f"  Error body: {res['body']}")
                overall_ok = False

    # datasets de Limite (limite_enc, limite_conv) -> doc LIM_DOC_ID (Limite TCMP)
    if not only_proj:
        # Janela YoY dinamica (mesma regra do v6): enquanto o mes vigente NAO bateu o batch de
        # encendido (pre-batch), esconde o mes vigente E recua o YoY 1 mes, p/ manter o comparativo
        # do ULTIMO mes exibido. Ex.: ago pre-batch -> mostra Jul/25 (YoY) + Jan..Jul/26 (esconde ago;
        # YoY vira Jul/25 em vez de Ago/25). Quando ago bate o batch, volta ao padrao (Ago/25 + ..Ago/26).
        _lim_prebatch = not current_month_batched()
        for name, sql in LIM_QUERIES.items():
            if only_names and name not in only_names:
                continue
            if _lim_prebatch:
                sql = (sql
                    .replace("INTERVAL 12 MONTH", "INTERVAL 13 MONTH")
                    .replace("INTERVAL 11 MONTH", "INTERVAL 12 MONTH")
                    .replace("DATE_ADD(DATE_TRUNC(CURRENT_DATE(),MONTH), INTERVAL 1 MONTH)",
                             "DATE_TRUNC(CURRENT_DATE(),MONTH)"))
            print(f"\n--- {name} --> {LIM_DOC_ID} ---")
            raw_path = TMP_DIR / f"{name}_raw.json"
            t0 = time.time()
            try:
                run_bq(sql, raw_path)
            except Exception as e:
                print(f"  [FAIL] bq query: {e}")
                overall_ok = False
                continue
            t_bq = time.time() - t0
            rows = to_numbers(json.load(open(raw_path, "r", encoding="utf-8")))
            print(f"  rows={len(rows):,}  bq_time={t_bq:.1f}s")
            t0 = time.time()
            res = upload_dataset(name, rows, doc_id=LIM_DOC_ID)
            status = "[OK]" if res["ok"] else "[FAIL]"
            print(f"  {status} upload http={res['http']}  size={res['size_mb']} MB  up_time={time.time()-t0:.1f}s")
            if not res["ok"]:
                print(f"  Error body: {res['body']}")
                overall_ok = False

    if only_lim or (only_names and "projecao" not in only_names):
        print(f"\n[push_datasets] End {time.strftime('%Y-%m-%d %H:%M:%S')} | overall={'OK' if overall_ok else 'FAIL'}")
        return

    # dataset projecao (bundle pos-processado pelo builder local) -> doc PROJ_DOC_ID (satelite Projecao)
    print(f"\n--- projecao --> {PROJ_DOC_ID} ---")
    t0 = time.time()
    try:
        res = push_projecao(rebuild=rebuild_proj)
        t = time.time() - t0
        status = "[OK]" if res["ok"] else "[FAIL]"
        print(f"  {status} PUT http={res['http']}  size={res['size_mb']} MB  time={t:.1f}s")
        if not res["ok"]:
            print(f"  Error body: {res['body']}")
            overall_ok = False
    except Exception as e:
        print(f"  [FAIL] projecao: {e}")
        overall_ok = False

    print(f"\n[push_datasets] End {time.strftime('%Y-%m-%d %H:%M:%S')} | overall={'OK' if overall_ok else 'FAIL'}")
    sys.exit(0 if overall_ok else 1)


if __name__ == "__main__":
    main()
