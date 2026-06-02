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
# 5. MONTHS_ACTUAL / MONTHS_PROJ — separado por mes
# ============================================================
# SQL `past` agora retorna actuals do mes atual + mes anterior (backfill
# automatico das ultimas datas do mes anterior — robusto a falhas no fim de mes).
prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
prev_key = f'{prev_year}-{prev_month:02d}'
cur_key  = f'{year}-{month:02d}'

def _slice_actuals(prefix):
    out = {}
    for tc in TC_KEYS:
        out[tc] = {}
        for sg in actual_data[tc]:
            d = {dia: v for dia, v in actual_data[tc][sg].items() if dia.startswith(prefix)}
            if d:
                out[tc][sg] = d
    return out

months_actual_jsobj = {
    cur_key:  _slice_actuals(cur_key),
    prev_key: _slice_actuals(prev_key),
}
months_proj_jsobj = {cur_key: {tc: {sg: dict(proj_data[tc][sg]) for sg in proj_data[tc]} for tc in TC_KEYS}}

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

# Lemos o existing MONTHS_ACTUAL e MONTHS_PROJ e atualizamos a chave dada,
# preservando dados de meses anteriores.
def update_months_const(html, var_name, new_for_key, key):
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
    cur_obj[key] = new_for_key
    new_str = f"const {var_name}={json.dumps(cur_obj, ensure_ascii=False)};"
    return html.replace(m.group(0), new_str)

html = update_months_const(html, 'MONTHS_ACTUAL', months_actual_jsobj[cur_key],  cur_key)
print(f'  OK   MONTHS_ACTUAL[{cur_key}]')
# Backfill do mes anterior — pega os ultimos dias que o scheduler perdeu
if months_actual_jsobj[prev_key] and any(months_actual_jsobj[prev_key][tc] for tc in TC_KEYS):
    html = update_months_const(html, 'MONTHS_ACTUAL', months_actual_jsobj[prev_key], prev_key)
    print(f'  OK   MONTHS_ACTUAL[{prev_key}] (backfill)')
html = update_months_const(html, 'MONTHS_PROJ', months_proj_jsobj[cur_key], cur_key)
print(f'  OK   MONTHS_PROJ[{cur_key}]')

# ============================================================
# 6a. BOOTSTRAP de novo mes (MONTHS_META + MONTHS_HIST + botao + header)
# ============================================================
cur_label = f'{MONTH_ABBR[month]}/{year}'

def bootstrap_months_meta(html, key, yr, mo):
    """Se MONTHS_META[key] nao existe, insere a entry no bloco."""
    block = re.search(r"const MONTHS_META\s*=\s*\{[^;]+?\};", html, flags=re.S)
    if not block:
        print('  WARN: MONTHS_META block not found')
        return html
    if re.search(rf"'{key}'\s*:", block.group(0)):
        return html
    _, n = monthrange(yr, mo)
    days = [date(yr, mo, d).isoformat() for d in range(1, n + 1)]
    labels = [f"{d[8:10]}/{MONTH_ABBR[mo]}" for d in days]
    entry = ("  '{k}': {{ label:'{lbl}', allDays:{days}, labels:{labels}, "
             "todayStr:'{today}', hasPrev:true }}").format(
        k=key, lbl=f'{MONTH_ABBR[mo]}/{yr}',
        days=json.dumps(days), labels=json.dumps(labels), today=TODAY)
    # Insere logo antes do `};` que fecha o objeto MONTHS_META
    new_block = re.sub(r"(\n\};)$", f",\n{entry}\\1", block.group(0), count=1)
    if new_block == block.group(0):
        print(f'  WARN: cant bootstrap MONTHS_META[{key}]')
        return html
    print(f'  OK   MONTHS_META[{key}] (bootstrap)')
    return html.replace(block.group(0), new_block)

def bootstrap_months_hist(html, key):
    """Garante MONTHS_HIST[key] existe (mesmo vazio) — evita crash em setMonth()."""
    if re.search(rf"'{key}'\s*:", html):
        # MONTHS_HIST usa aspas simples; o regex generico ja casa com qualquer ocorrencia.
        # Especifico: procurar no escopo do const MONTHS_HIST.
        pass
    m = re.search(r"const MONTHS_HIST\s*=\s*\{[^;]+?\};", html, flags=re.S)
    if not m:
        print('  WARN: MONTHS_HIST not found')
        return html
    if re.search(rf"'{key}'\s*:", m.group(0)):
        return html
    new_block = m.group(0).replace('};', f", '{key}': {{}} }};")
    print(f'  OK   MONTHS_HIST[{key}] (bootstrap)')
    return html.replace(m.group(0), new_block)

def bootstrap_month_button(html, key, label):
    """Adiciona botao do mes na .month-selector e troca o `active` pra ele."""
    if f'data-month="{key}"' in html:
        # Botao ja existe — apenas garante que ele e o unico com `active`
        html = re.sub(r'class="month-btn active"', 'class="month-btn"', html)
        html = re.sub(
            rf'class="month-btn"(\s+data-month="{key}")',
            rf'class="month-btn active"\1',
            html, count=1)
        print(f'  OK   month-btn[{key}] (already exists, marked active)')
        return html
    # Remove active de qualquer botao existente
    html = re.sub(r'class="month-btn active"', 'class="month-btn"', html)
    # Insere novo botao antes do </div> da month-selector
    new_btn = f'      <button class="month-btn active" data-month="{key}" onclick="setMonth(\'{key}\')">{label}</button>\n'
    new_html, n_sub = re.subn(
        r'(<div class="month-selector">[\s\S]*?)(\n\s*</div>)',
        lambda m: f"{m.group(1)}\n{new_btn}{m.group(2)}",
        html, count=1)
    if n_sub:
        print(f'  OK   month-btn[{key}] (bootstrap)')
        return new_html
    print(f'  WARN: cant insert month-btn[{key}]')
    return html

def update_active_month_state(html, key, label):
    """Atualiza let ACTIVE_MONTH e o header 'Mes vigente: XXX/YYYY'."""
    html, n1 = re.subn(r"(let\s+ACTIVE_MONTH\s*=\s*')[^']+(')", rf"\g<1>{key}\g<2>", html, count=1)
    if n1:
        print(f'  OK   ACTIVE_MONTH={key}')
    html, n2 = re.subn(r'(Mes vigente:\s*)[^&<]+', rf'\g<1>{label} ', html, count=1)
    if n2:
        print(f'  OK   header (Mes vigente: {label})')
    return html

html = bootstrap_months_meta(html, cur_key, year, month)
html = bootstrap_months_hist(html, cur_key)
html = bootstrap_month_button(html, cur_key, cur_label)
html = update_active_month_state(html, cur_key, cur_label)

# Atualiza todayStr do mes anterior pra dia 1 do mes corrente.
# Chart JS usa `d < TODAY` (estrito) pra classificar real vs projecao;
# pondo todayStr no dia 1 do mes seguinte garante que TODOS os dias do mes
# anterior (incluindo o ultimo) sejam tratados como real.
prev_close_day = date(year, month, 1).isoformat()
prev_close_pat = rf"('{prev_key}':\s*\{{[^}}]*?todayStr:')[^']+(')"
html, n_close = re.subn(prev_close_pat, lambda m: f"{m.group(1)}{prev_close_day}{m.group(2)}", html, count=1)
if n_close:
    print(f'  OK   todayStr[{prev_key}] -> {prev_close_day} (mes fechado)')

# Zera MONTHS_PROJ do mes anterior (mes ja fechado, projecao nao serve mais).
# Sem isso, alguns elementos podem renderizar a projecao velha em ferramentas
# que iteram tanto ACTUAL quanto PROJ.
html = update_months_const(html, 'MONTHS_PROJ',
                           {tc: {sg: {} for sg in SG_ORDER} for tc in TC_KEYS},
                           prev_key)
print(f'  OK   MONTHS_PROJ[{prev_key}] cleared (mes fechado)')

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

# todayStr do MONTHS_META do mes corrente: precisa apontar pra HOJE (TODAY)
# Sem isso, o grafico Emissoes trata os dias anteriores ao todayStr antigo
# como projecao em vez de real (bug: encendidos de ontem somem do grafico).
cur_meta_pat = rf"('{year}-{month:02d}':\s*\{{[^}}]*?todayStr:')[^']+(')"
html = patch(html, cur_meta_pat, (lambda m: f'{m.group(1)}{TODAY}{m.group(2)}'), f'todayStr {year}-{month:02d}')

# KPI labels com data
today_short = f'{TODAY[8:10]}/{MONTH_ABBR[month]}'
last_short  = f'{ALL_DAYS[-1][8:10]}/{MONTH_ABBR[month]}'
html = patch(html, r'(<div class="kpi-label">Projecao hoje \()[^)]*(\)</div>)', (lambda m: f'{m.group(1)}{today_short}{m.group(2)}'), 'Projecao hoje (date)')
html = patch(html, r'(<div class="kpi-sub">ate )[^<]*(</div>)', (lambda m: f'{m.group(1)}{last_short}{m.group(2)}'), 'ate (date)')

# pct do mes concluido
for slug, tc in [('full','TC Full'),('micro','Micro TC')]:
    pct = kpis[tc]['pct_real']
    # patch o kpi-sub seguinte ao kv-real-{slug}
    pat = rf'(id="kv-real-{slug}">[^<]*</div>\s*<div class="kpi-sub">)[^<]*(</div>)'
    html = patch(html, pat, (lambda m, v=f'{pct}% do mes concluido': f'{m.group(1)}{v}{m.group(2)}'), f'pct-real-{slug}')

HTML_FILE.write_text(html, encoding='utf-8')
print('index.html atualizado.')
print(f'\nResumo {TODAY}:')
for tc in TC_KEYS:
    k = kpis[tc]
    print(f"  {tc:9s}  real={k['real_total']:>10,}  proj={k['proj_total']:>10,}  total={k['grand_total']:>10,}")
print('\nProximo passo: git add . && git commit && git push')
