#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
update_dashboard.py — UNICO entrypoint para atualizar o dashboard.

O QUE FAZ:
  - Roda a query Emissoes (dashboard_sg.sql) no BigQuery
  - Atualiza proj_historico.json com a projecao de hoje
  - APLICA PATCHES IN-PLACE no index.html (NAO regenera o arquivo)
    -> preserva a aba Encendidos e qualquer outra mudanca de UI

POR QUE EM PATCHES:
  O run_dashboard.py antigo regenerava o HTML do zero, sobrescrevendo
  toda a aba Encendidos (montada pelo Claude Cowork). Este script
  altera APENAS os campos de dados Emissoes.

COMO RODAR:
  cd C:\\Users\\GPEREIRADOSS\\dashboard-emissoes
  py update_dashboard.py

PROXIMOS PASSOS POS-RUN:
  git add . && git commit -m "Dashboard YYYY-MM-DD" && git push
"""
import subprocess, json, csv, io, os, sys, re
from datetime import date, timedelta
from calendar  import monthrange
from collections import defaultdict
from pathlib import Path

# ============================================================
# CONFIG
# ============================================================
BPY        = r'C:\Users\GPEREIRADOSS\AppData\Local\Google\Cloud SDK\google-cloud-sdk\platform\bundledpython\python.exe'
BQ_CMD     = r'C:\Users\GPEREIRADOSS\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin\bq.cmd'
PROJECT_ID = 'meli-bi-data'
REPO       = Path(__file__).resolve().parent
SQL_FILE   = REPO / 'dashboard_sg.sql'
HIST_FILE  = REPO / 'proj_historico.json'
HTML_FILE  = REPO / 'index.html'

TODAY     = date.today().isoformat()
YESTERDAY = (date.today() - timedelta(1)).isoformat()
year, month = date.today().year, date.today().month
_, n_days   = monthrange(year, month)
ALL_DAYS    = [date(year, month, d).isoformat() for d in range(1, n_days + 1)]
MONTH_ABBR  = {1:'Jan',2:'Fev',3:'Mar',4:'Abr',5:'Mai',6:'Jun',
               7:'Jul',8:'Ago',9:'Set',10:'Out',11:'Nov',12:'Dez'}

TC_KEYS  = ['TC Full', 'Micro TC']
# Super-grupos que o SQL devolve (match com o JS index.html SG_ORDER)
SG_ORDER = ['BAU', 'EA', 'Sellers', 'Cuentas Canceladas', 'Only Nav', 'Mar Aberto']

def fmt(n):
    return f"{int(round(n)):,}".replace(',', '.')

# ============================================================
# 1. BQ
# ============================================================
print(f"[{TODAY}] Rodando query Emissoes no BQ...")
env = os.environ.copy()
env['CLOUDSDK_PYTHON'] = BPY
with open(SQL_FILE, 'rb') as f:
    result = subprocess.run(
        [BQ_CMD, 'query', f'--project_id={PROJECT_ID}',
         '--use_legacy_sql=false', '--nouse_cache', '--format=csv',
         '--max_rows=10000'],
        stdin=f, capture_output=True, env=env, shell=True)
if result.returncode != 0:
    print('ERRO BQ:', result.stderr.decode(errors='replace'))
    sys.exit(1)
raw = result.stdout.decode('utf-8', errors='replace')
print(f"  -> {len(raw.splitlines())-1} linhas")

# ============================================================
# 2. PARSE
# ============================================================
actual_data = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
proj_data   = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))

reader = csv.DictReader(io.StringIO(raw))
for row in reader:
    tc  = 'TC Full' if 'Full' in row['FLAG_TC'] else 'Micro TC'
    sg  = row['super_grupo'] if row['super_grupo'] in SG_ORDER else 'BAU'
    dia = row['dia'][:10]
    val = float(row['total'] or 0)
    if row['tipo'] == 'actual':
        actual_data[tc][sg][dia] += val
    else:
        proj_data[tc][sg][dia] += val

# ============================================================
# 3. HISTORICO
# ============================================================
if HIST_FILE.exists():
    historico = json.loads(HIST_FILE.read_text(encoding='utf-8'))
else:
    historico = {}
historico[TODAY] = {tc: {sg: dict(proj_data[tc][sg]) for sg in proj_data[tc]} for tc in TC_KEYS}
HIST_FILE.write_text(json.dumps(historico, ensure_ascii=False, indent=2), encoding='utf-8')

# ============================================================
# 4. KPIs
# ============================================================
actual_days = [d for d in ALL_DAYS if d < TODAY]
proj_days   = [d for d in ALL_DAYS if d >= TODAY]
kpis = {}
for tc in TC_KEYS:
    real_total = sum(actual_data[tc][sg].get(d, 0) for sg in SG_ORDER for d in actual_days)
    proj_total = sum(proj_data[tc][sg].get(d, 0) for sg in SG_ORDER for d in proj_days)
    hoje_proj  = sum(proj_data[tc][sg].get(TODAY, 0) for sg in SG_ORDER)
    kpis[tc] = dict(
        real_total = int(real_total),
        proj_total = int(proj_total),
        grand_total = int(real_total + proj_total),
        hoje_proj  = int(hoje_proj),
        pct_real   = round(real_total / max(real_total + proj_total, 1) * 100, 1),
    )

# ============================================================
# 5. MONTHS_HIST (de proj_historico de ontem) — para a linha tracejada
# ============================================================
months_actual_jsobj = {}  # {'2026-04': {...}, '2026-05': {...}}

# Mes anterior (-1 mes) — pega ENC histórico do BQ separadamente, ja temos em actual_data se rodou no inicio do mes
# Pra simplificar: keep apenas o mes corrente em actual_data; mes anterior fica no que ja estava no HTML
months_actual_jsobj[f'{year}-{month:02d}'] = {tc: {sg: dict(actual_data[tc][sg]) for sg in actual_data[tc]} for tc in TC_KEYS}

months_proj_jsobj = {f'{year}-{month:02d}': {tc: {sg: dict(proj_data[tc][sg]) for sg in proj_data[tc]} for tc in TC_KEYS}}

# ============================================================
# 6. PATCH index.html (in-place)
# ============================================================
print('Patching index.html...')
html = HTML_FILE.read_text(encoding='utf-8')

def patch(html, pattern, replacement, label, flags=0):
    n = len(re.findall(pattern, html, flags))
    if n == 0:
        print(f'  WARN: no match for {label}')
        return html
    html = re.sub(pattern, replacement, html, count=n, flags=flags)
    print(f'  OK   {label} ({n})')
    return html

# Helper pra escapar barras invertidas em replacements
def lit(s): return lambda m: s

cur_key = f'{year}-{month:02d}'

# Lemos o existing MONTHS_ACTUAL e MONTHS_PROJ e atualizamos APENAS o mes corrente,
# preservando dados de meses anteriores.
def update_months_const(html, var_name, new_for_cur, cur_key):
    m = re.search(rf"const {var_name}\s*=\s*(\{{[^;]+?\}})\s*;", html)
    if not m:
        print(f'  WARN: {var_name} not found')
        return html
    try:
        # O bloco entre {...} eh JSON-like com aspas duplas. Da pra parsear.
        cur_obj = json.loads(m.group(1).replace("'", '"'))
    except Exception as e:
        print(f'  WARN: cant parse {var_name}: {e}')
        return html
    cur_obj[cur_key] = new_for_cur
    new_str = f"const {var_name}={json.dumps(cur_obj, ensure_ascii=False)};"
    return html.replace(m.group(0), new_str)

html = update_months_const(html, 'MONTHS_ACTUAL', months_actual_jsobj[cur_key], cur_key)
print('  OK   MONTHS_ACTUAL')
html = update_months_const(html, 'MONTHS_PROJ', months_proj_jsobj[cur_key], cur_key)
print('  OK   MONTHS_PROJ')

# KPIs
for tc, slug in [('TC Full','full'), ('Micro TC','micro')]:
    k = kpis[tc]
    html = patch(html, rf'(id="kv-real-{slug}">)[^<]*(</div>)',  (lambda m, v=fmt(k['real_total']): f'{m.group(1)}{v}{m.group(2)}'), f'kv-real-{slug}')
    html = patch(html, rf'(id="kv-hoje-{slug}">)[^<]*(</div>)',  (lambda m, v=fmt(k['hoje_proj']): f'{m.group(1)}{v}{m.group(2)}'), f'kv-hoje-{slug}')
    html = patch(html, rf'(id="kv-proj-{slug}">)[^<]*(</div>)',  (lambda m, v=fmt(k['proj_total']): f'{m.group(1)}{v}{m.group(2)}'), f'kv-proj-{slug}')
    html = patch(html, rf'(id="kv-total-{slug}">)[^<]*(</div>)', (lambda m, v=fmt(k['grand_total']): f'{m.group(1)}{v}{m.group(2)}'), f'kv-total-{slug}')

# Grand totals
gt_real = kpis['TC Full']['real_total'] + kpis['Micro TC']['real_total']
gt_proj = kpis['TC Full']['proj_total'] + kpis['Micro TC']['proj_total']
gt_total = gt_real + gt_proj
html = patch(html, r'(id="gt-real">)[^<]*(</div>)',  (lambda m: f'{m.group(1)}{fmt(gt_real)}{m.group(2)}'),  'gt-real')
html = patch(html, r'(id="gt-proj">)[^<]*(</div>)',  (lambda m: f'{m.group(1)}{fmt(gt_proj)}{m.group(2)}'),  'gt-proj')
html = patch(html, r'(id="gt-total">)[^<]*(</div>)', (lambda m: f'{m.group(1)}{fmt(gt_total)}{m.group(2)}'), 'gt-total')

# Atualizado date
today_label = f'{TODAY[8:10]}/{MONTH_ABBR[month]}/{TODAY[:4]}'
html = patch(html, r'(Atualizado:\s*<span>)[^<]*(</span>)', (lambda m: f'{m.group(1)}{today_label}{m.group(2)}'), 'Atualizado')

# KPI labels com data
today_short = f'{TODAY[8:10]}/{MONTH_ABBR[month]}'
last_short  = f'{ALL_DAYS[-1][8:10]}/{MONTH_ABBR[month]}'
html = patch(html, r'(<div class="kpi-label">Projecao hoje \()[^)]*(\)</div>)', (lambda m: f'{m.group(1)}{today_short}{m.group(2)}'), 'Projecao hoje (date)')
html = patch(html, r'(<div class="kpi-sub">ate )[^<]*(</div>)', (lambda m: f'{m.group(1)}{last_short}{m.group(2)}'), 'ate (date)')

# pct do mes concluido
for slug, tc in [('full','TC Full'),('micro','Micro TC')]:
    pct = kpis[tc]['pct_real']
    # patch o kpi-sub seguinte ao kv-real-{slug}
    pat = rf'(id="kv-real-{slug}">[^<]*</div><div class="kpi-sub">)[^<]*(</div>)'
    html = patch(html, pat, (lambda m, v=f'{pct}% do mes concluido': f'{m.group(1)}{v}{m.group(2)}'), f'pct-real-{slug}')

HTML_FILE.write_text(html, encoding='utf-8')
print('index.html atualizado.')
print(f'\nResumo {TODAY}:')
for tc in TC_KEYS:
    k = kpis[tc]
    print(f"  {tc:9s}  real={k['real_total']:>10,}  proj={k['proj_total']:>10,}  total={k['grand_total']:>10,}")
print('\nProximo passo: git add . && git commit && git push')
