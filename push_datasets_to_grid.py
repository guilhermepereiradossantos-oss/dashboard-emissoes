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
# Projeto de EXECUCAO/billing dos jobs BQ. Usar o furyid do usuario (nao o meli-bi-data
# compartilhado) p/ evitar "max jobs queued per project" na fila lotada do projeto comum.
# Os jobs leem meli-bi-data.* cross-project normalmente.
PROJECT_ID = "ddme000725-g9rtvpqr28z-furyid"
GRID_HOST = "https://grid.melioffice.com"
TMP_DIR = Path(r"C:\Users\GPEREIRADOSS\grid_tmp")
TMP_DIR.mkdir(exist_ok=True)

# Acima deste tamanho, sobe pelo fluxo v2 de URL assinada (upload-url -> PUT -> publish),
# que vai direto pro storage e evita 503/timeout do PUT simples do proxy /data/{name}.
# Abaixo, usa o PUT legado /data/{name} (testado e estavel p/ datasets pequenos).
SIGNED_THRESHOLD_MB = 20

# Datasets que sobem em json_columnar (coluna 1x + dados em arrays) p/ economizar ~62%.
# Os demais continuam json_rows. O HTML (fromColumnar) aceita os dois formatos.
COLUMNAR = {"mensal_encendidos", "mensal_emissoes"}

# Caso de "string -> numero" depois de bq query --format=prettyjson
NUM_COLS = {"n_enc", "n_primo", "n_reenc", "n_conv", "soma_limite"}

QUERIES = {
    "mensal_encendidos": """
SELECT
  FORMAT_DATE("%Y-%m", DT_ENCENDIDO) AS safra_enc,
  FLAG_TC, FLAG_REENCENDIDO, FLAG_NISE,
  FLAG_CANAL_AQUISICAO AS canal_aquisicao,
  FLAG_USO_CC_ANT_ENC_TC,
  COALESCE(FLAG_APP_ATIVO, "Sem App") AS FLAG_APP_ATIVO,
  status_cancelada_no_mes_de_encendido,
  CASE
    WHEN FLAG_NISE = "0. SELLER" THEN "Sellers"
    WHEN grupo_especial LIKE "%Mar Aberto%" THEN "Mar Aberto"
    WHEN grupo_especial = "TEST REACH-TEST NO ECOSISTEMATICOS" THEN "Only Nav"
    WHEN grupo_especial LIKE "%CANCELADAS%" OR status_cancelada_anteriormente = TRUE THEN "Cuentas Canceladas"
    ELSE "BAU"
  END AS super_grupo,
  CASE
    WHEN rating_tc = "A1" THEN "A1" WHEN rating_tc = "A2" THEN "A2" WHEN rating_tc = "A" THEN "A3"
    WHEN rating_tc = "B1" THEN "B1" WHEN rating_tc = "B2" THEN "B2" WHEN rating_tc IN ("B","B3") THEN "B3"
    WHEN rating_tc IN ("C","C1","C2","C3") THEN "C"
    WHEN rating_tc IN ("D","E","F","G","J","J1","J2") THEN "D-J"
    WHEN rating_tc IS NULL OR rating_tc = "Z" THEN "Sem rating"
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
    "mensal_emissoes": """
SELECT
  FORMAT_DATE("%Y-%m", DT_CONV) AS safra_conv,
  FLAG_TC, FLAG_REENCENDIDO, FLAG_NISE,
  FLAG_CANAL_AQUISICAO AS canal_aquisicao,
  FLAG_USO_CC_ANT_ENC_TC,
  COALESCE(FLAG_APP_ATIVO, "Sem App") AS FLAG_APP_ATIVO,
  status_cancelada_no_mes_de_encendido,
  CASE
    WHEN FLAG_NISE = "0. SELLER" THEN "Sellers"
    WHEN grupo_especial LIKE "%Mar Aberto%" THEN "Mar Aberto"
    WHEN grupo_especial = "TEST REACH-TEST NO ECOSISTEMATICOS" THEN "Only Nav"
    WHEN grupo_especial LIKE "%CANCELADAS%" OR status_cancelada_anteriormente = TRUE THEN "Cuentas Canceladas"
    ELSE "BAU"
  END AS super_grupo,
  CASE
    WHEN rating_tc = "A1" THEN "A1" WHEN rating_tc = "A2" THEN "A2" WHEN rating_tc = "A" THEN "A3"
    WHEN rating_tc = "B1" THEN "B1" WHEN rating_tc = "B2" THEN "B2" WHEN rating_tc IN ("B","B3") THEN "B3"
    WHEN rating_tc IN ("C","C1","C2","C3") THEN "C"
    WHEN rating_tc IN ("D","E","F","G","J","J1","J2") THEN "D-J"
    WHEN rating_tc IS NULL OR rating_tc = "Z" THEN "Sem rating"
    ELSE "Outros" END AS rating_tc_grp,
  SUM(QTDE) AS n_conv,
  SUM(soma_limite) AS soma_limite
FROM `meli-bi-data.SBOX_CREDITSTC.base_projecao_Gui`
WHERE FLAG_CONVERSAO = "1. Convertido"
  AND DT_CONV >= DATE_SUB(DATE_TRUNC(CURRENT_DATE(), MONTH), INTERVAL 12 MONTH)
GROUP BY ALL
""",
    "diario": """
SELECT
  CAST(DT_ENCENDIDO AS STRING) AS dia_enc,
  FORMAT_DATE("%Y-%m", DT_ENCENDIDO) AS safra_enc,
  FLAG_TC,
  CASE
    WHEN FLAG_NISE = "0. SELLER" THEN "Sellers"
    WHEN grupo_especial LIKE "%Mar Aberto%" THEN "Mar Aberto"
    WHEN grupo_especial = "TEST REACH-TEST NO ECOSISTEMATICOS" THEN "Only Nav"
    WHEN grupo_especial LIKE "%CANCELADAS%" OR status_cancelada_anteriormente = TRUE THEN "Cuentas Canceladas"
    ELSE "BAU"
  END AS super_grupo,
  SUM(QTDE) AS n_enc
FROM `meli-bi-data.SBOX_CREDITSTC.base_projecao_Gui`
WHERE DT_ENCENDIDO >= DATE_SUB(DATE_TRUNC(CURRENT_DATE(), MONTH), INTERVAL 12 MONTH)
GROUP BY ALL
ORDER BY dia_enc
""",
    # Datasets dedicados (pizza Nº Propostas) — pequenos, respondem só a Safra + Tipo TC.
    "nprop_enc": """
SELECT
  FORMAT_DATE("%Y-%m", DT_ENCENDIDO) AS safra_enc,
  FLAG_TC, range_numero_propostas,
  SUM(QTDE) AS n_enc
FROM `meli-bi-data.SBOX_CREDITSTC.base_projecao_Gui`
WHERE DT_ENCENDIDO >= DATE_SUB(DATE_TRUNC(CURRENT_DATE(), MONTH), INTERVAL 12 MONTH)
GROUP BY ALL
""",
    "nprop_emi": """
SELECT
  FORMAT_DATE("%Y-%m", DT_CONV) AS safra_conv,
  FLAG_TC, range_numero_propostas,
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
  CASE
    WHEN FLAG_NISE = "0. SELLER" THEN "Sellers"
    WHEN grupo_especial LIKE "%Mar Aberto%" THEN "Mar Aberto"
    WHEN grupo_especial = "TEST REACH-TEST NO ECOSISTEMATICOS" THEN "Only Nav"
    WHEN grupo_especial LIKE "%CANCELADAS%" OR status_cancelada_anteriormente = TRUE THEN "Cuentas Canceladas"
    ELSE "BAU"
  END AS super_grupo,
  CASE
    WHEN rating_tc = "A1" THEN "A1" WHEN rating_tc = "A2" THEN "A2" WHEN rating_tc = "A" THEN "A3"
    WHEN rating_tc = "B1" THEN "B1" WHEN rating_tc = "B2" THEN "B2" WHEN rating_tc IN ("B","B3") THEN "B3"
    WHEN rating_tc IN ("C","C1","C2","C3") THEN rating_tc
    WHEN rating_tc IN ("D","E","F","G","J","J1","J2") THEN "D-J"
    WHEN rating_tc IS NULL OR rating_tc = "Z" THEN "Sem rating"
    ELSE "Outros" END AS rating_tc_grp,
  range_numero_propostas,
  COALESCE(FLAG_APP_ATIVO, "Sem App") AS FLAG_APP_ATIVO,
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
    _set_definition_format(base, fmt)

    # 1) URL assinada (+ revision)
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

    # 2) PUT do conteudo no upload_url (storage; responde 204)
    rp = subprocess.run(
        [CURL_CMD, "-s", "-w", "\n%{http_code}", "-X", "PUT", upload_url,
         "-H", "Content-Type: application/json", "--data-binary", f"@{payload_path}"],
        capture_output=True, text=True,
    )
    put_code = rp.stdout.strip().rsplit("\n", 1)[-1]
    if put_code not in ("200", "201", "204"):
        return {"size_mb": round(size_mb, 2), "http": f"put-{put_code}", "ok": False, "body": rp.stdout[:300]}

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
    print(f"  [batch-check] enc vigente={int(cur):,} vs anterior={int(prev):,}"
          f" -> {'BATCH OK (mostra mes vigente)' if batched else 'PRE-BATCH (esconde mes vigente das abas v6)'}".replace(",", "."))
    return batched


def main():
    # Push seletivo (one-off):
    #   python push_datasets_to_grid.py                 -> tudo (5 datasets -> DOC_ID, projecao -> PROJ_DOC_ID)
    #   python push_datasets_to_grid.py projecao         -> so projecao (rebuild) -> PROJ_DOC_ID
    #   python push_datasets_to_grid.py projecao --no-rebuild  -> so projecao reusando _proj_data.json
    args = [a for a in sys.argv[1:]]
    only_proj = "projecao" in args
    rebuild_proj = "--no-rebuild" not in args

    print(f"[push_datasets] Start {time.strftime('%Y-%m-%d %H:%M:%S')}"
          + (f" (SO projecao, rebuild={rebuild_proj})" if only_proj else ""))
    overall_ok = True

    if not only_proj:
        exclude_cur = not current_month_batched()  # esconde mês vigente das abas v6 até o batch
        for name, sql in QUERIES.items():  # 6 datasets -> doc principal (Emissoes+Encendidos)
            if exclude_cur and name in EXCL_COL:
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
