#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_projecao_local.py — Gera os dados da aba Projecao LOCALMENTE.

NAO publica nada:
  - roda dashboard_sg_gui.sql (base do usuario, via proj_base_slim_gui)
  - aplica os mesmos ajustes do update_dashboard.py (manuais, TARGET, historico)
  - escreve _proj_data.json (consumido pela aba de teste _grid_v7_proj.html)
  - NAO faz patch de HTML, NAO faz git, NAO escreve no proj_historico.json de
    producao (snapshot de hoje vai pra _proj_historico_local.json).

COMO RODAR:
  cd C:\\Users\\GPEREIRADOSS\\dashboard-emissoes
  py build_projecao_local.py
"""
import subprocess, json, csv, io, os, sys
from datetime import date, timedelta
from calendar import monthrange
from collections import defaultdict
from pathlib import Path

# ---- CONFIG (mesma do update_dashboard.py) ----
BPY        = r'C:\Users\GPEREIRADOSS\AppData\Local\Google\Cloud SDK\google-cloud-sdk\platform\bundledpython\python.exe'
BQ_CMD     = r'C:\Users\GPEREIRADOSS\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin\bq.cmd'
PROJECT_ID = 'ddme000725-g9rtvpqr28z-furyid'
REPO       = Path(__file__).resolve().parent
SQL_FILE   = REPO / 'dashboard_sg_gui.sql'
HIST_FILE  = REPO / 'proj_historico.json'         # leitura (producao, read-only)
HIST_LOCAL = REPO / '_proj_historico_local.json'  # escrita (snapshots de dev)
OUT_FILE   = REPO / '_proj_data.json'

TODAY     = date.today().isoformat()
year, month = date.today().year, date.today().month
_, n_days   = monthrange(year, month)
ALL_DAYS    = [date(year, month, d).isoformat() for d in range(1, n_days + 1)]
MONTH_ABBR  = {1:'Jan',2:'Fev',3:'Mar',4:'Abr',5:'Mai',6:'Jun',
               7:'Jul',8:'Ago',9:'Set',10:'Out',11:'Nov',12:'Dez'}
TC_KEYS  = ['TC Full', 'Micro TC']
SG_ORDER = ['BAU', 'EA', 'Sellers', 'Cuentas Canceladas', 'Only Nav', 'Mar Aberto']

# Mesmos overrides/skip do update_dashboard.py
# 2026-06-25: TARGET fixo de TC Full (325k) DESLIGADO a pedido do usuario para a projecao
# voltar a ser organica (escopo "minimo": mantidos ajustes manuais Python, spike_override e -15k).
# Para reativar uma trava de TARGET: TARGET_TOTALS = {'2026-06': {'TC Full': <valor>}}
TARGET_TOTALS = {}
SKIP_HIST_SNAPSHOTS = {'2026-06-01', '2026-06-02', '2026-06-25'}  # 25/06: snapshot dev ruim (Micro TC HIST degenerado)

def fmt(n): return f"{int(round(n)):,}".replace(',', '.')

# ============================================================
# 1. BQ — roda a query da base nova
# ============================================================
print(f"[{TODAY}] Rodando dashboard_sg_gui.sql (base proj_base_slim_gui)...")
env = os.environ.copy(); env['CLOUDSDK_PYTHON'] = BPY
with open(SQL_FILE, 'rb') as f:
    result = subprocess.run(
        [BQ_CMD, 'query', f'--project_id={PROJECT_ID}',
         '--use_legacy_sql=false', '--nouse_cache', '--format=csv', '--max_rows=10000'],
        stdin=f, capture_output=True, env=env, shell=True)
if result.returncode != 0:
    print('ERRO BQ:', result.stderr.decode(errors='replace')); sys.exit(1)
raw = result.stdout.decode('utf-8', errors='replace')
print(f"  -> {len(raw.splitlines())-1} linhas")

# ============================================================
# 2. PARSE
# ============================================================
actual_data = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
proj_data   = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
for row in csv.DictReader(io.StringIO(raw)):
    tc  = 'TC Full' if 'Full' in row['FLAG_TC'] else 'Micro TC'
    sg  = row['super_grupo'] if row['super_grupo'] in SG_ORDER else 'BAU'
    dia = row['dia'][:10]
    val = float(row['total'] or 0)
    (actual_data if row['tipo'] == 'actual' else proj_data)[tc][sg][dia] += val

# ============================================================
# 2a. AJUSTES MANUAIS (identico ao update_dashboard.py)
# ============================================================
cur_key = f'{year}-{month:02d}'
# 2a DESLIGADO (2026-06-26): ajustes manuais de Micro TC (BAU 24-26 / Sellers 17-25)
# removidos a pedido do usuario -> projecao ORGANICA pura. (cur_key mantido; usado em 2b/secao 5.)

# ============================================================
# 2b. ESCALA PRA TARGET (identico)
# ============================================================
_cfg = TARGET_TOTALS.get(cur_key)
if _cfg:
    for tc in TC_KEYS:
        target = _cfg.get(tc)
        if not target: continue
        real_so_far = sum(v for sg in actual_data[tc] for d, v in actual_data[tc][sg].items() if d.startswith(cur_key))
        proj_raw = sum(v for sg in proj_data[tc] for d, v in proj_data[tc][sg].items() if d.startswith(cur_key))
        proj_target = target - real_so_far
        if proj_raw > 0 and proj_target > 0:
            factor = proj_target / proj_raw
            for sg in proj_data[tc]:
                for d in list(proj_data[tc][sg].keys()):
                    if d.startswith(cur_key): proj_data[tc][sg][d] *= factor
            print(f'  TARGET {tc}: real={int(real_so_far):,} + proj_raw={int(proj_raw):,} -> alvo={int(proj_target):,} (f={factor:.4f})')
        elif proj_target <= 0:
            for sg in proj_data[tc]:
                for d in list(proj_data[tc][sg].keys()):
                    if d.startswith(cur_key): proj_data[tc][sg][d] = 0
            print(f'  TARGET {tc}: real>=target; proj zerada')

# ============================================================
# 2c. SAZONALIDADE DIA-DA-SEMANA (+ nivel TC Full no run-rate do mes)
#   Pedido do usuario (2026-06-26): a projecao organica nao tinha sazonalidade
#   semanal (o hazard ignora weekday) -> fim de semana vinha igual a dia util, e o
#   nivel do TC Full ficava ~12% acima do run-rate realizado.
#   - Fatores DOW por TC: mediana diaria por dia-da-semana (ult. ~90d realizados,
#     robusta a spikes de batch), normalizada p/ media 1.0.
#   - TC Full: nivel-alvo do dia = run-rate do mes * fator_DOW(dia) (reescala a proj
#     organica do dia preservando o mix de super_grupo). Derruba nivel e fds.
#   - Micro TC: preserva a SOMA organica futura, so redistribui pelos fatores DOW
#     (fds caem, dias uteis sobem; total ~igual — Micro ja esta no run-rate).
# ============================================================
def _median(xs):
    s = sorted(xs); k = len(s)
    if not k: return 0.0
    return s[k // 2] if k % 2 else (s[k // 2 - 1] + s[k // 2]) / 2

def dow_factors(tc):
    """mediana diaria por weekday (0=Seg..6=Dom), ult. 90d realizados, normalizada mean=1."""
    daily = defaultdict(float)
    for sg in actual_data[tc]:
        for d, v in actual_data[tc][sg].items():
            daily[d] += v
    cutoff = (date.today() - timedelta(days=90)).isoformat()
    byw = defaultdict(list)
    for d, v in daily.items():
        if cutoff <= d < TODAY and v > 0:
            byw[date.fromisoformat(d).weekday()].append(v)
    med = {wd: _median(vs) for wd, vs in byw.items() if vs}
    mean_med = sum(med.values()) / len(med) if med else 0
    return {wd: med[wd] / mean_med for wd in med} if mean_med > 0 else {}

def _apply_day_target(tc, d, target):
    """forca a soma do dia d (todos SG) = target, reescalando a proj organica (preserva mix)."""
    organic = sum(proj_data[tc][sg].get(d, 0) for sg in proj_data[tc])
    if organic > 0:
        f = target / organic
        for sg in proj_data[tc]:
            if d in proj_data[tc][sg]: proj_data[tc][sg][d] *= f
    else:
        proj_data[tc]['BAU'][d] = target

def _daily_real(tc):
    """soma diaria realizada (todos SG), por dia < hoje."""
    daily = defaultdict(float)
    for sg in actual_data[tc]:
        for d, v in actual_data[tc][sg].items():
            if d < TODAY: daily[d] += v
    return daily

def month_runrate(tc):
    """media diaria realizada do mes vigente (run-rate do mes)."""
    daily = _daily_real(tc)
    vals = [v for d, v in daily.items() if d.startswith(cur_key) and v > 0]
    return sum(vals) / len(vals) if vals else 0

def recent_runrate(tc, ndays=7):
    """mediana das ultimas ndays diarias realizadas (robusta a spikes). Capta o
    decaimento de fim de mes (ex.: Micro cai do começo p/ o fim)."""
    daily = _daily_real(tc)
    last = [daily[d] for d in sorted(daily)][-ndays:]
    return _median(last) if last else 0

# Ancora por TC: TC Full segue o run-rate do mes; Micro usa run-rate RECENTE
# (mediana ult.7d) porque decai ao longo do mes — a media do mes superestima a cauda.
ANCHOR_MODE = {'TC Full': 'month', 'Micro TC': 'recent'}
rem_days = [d for d in ALL_DAYS if d >= TODAY]
# 2026-07-06: DESLIGADO. run-rate+DOW+cap achatavam a linha na cauda decrescente do
# inicio do mes e ignoravam o pico pos-encendido, derrubando a projecao a cada realizado.
# Volta ao shape ORGANICO (proj_template alinhado ao pico + proj_organico + ma_proj).
APPLY_RUNRATE_DOW = False
if APPLY_RUNRATE_DOW:
    for tc in TC_KEYS:
        fac = dow_factors(tc)
        if not fac or not rem_days: continue
        wd_of = lambda d: date.fromisoformat(d).weekday()
        mode = ANCHOR_MODE.get(tc, 'month')
        anchor = month_runrate(tc) if mode == 'month' else recent_runrate(tc, 7)
        if anchor <= 0: continue
        for d in rem_days:
            _apply_day_target(tc, d, anchor * fac.get(wd_of(d), 1.0))
        proj_rem = sum(proj_data[tc][sg].get(d, 0) for sg in proj_data[tc] for d in rem_days)
        print(f'  DOW {tc}: anchor({mode})={int(anchor):,} -> proj_restante={int(proj_rem):,}'.replace(',', '.'))
    last_real = max((d for d in ALL_DAYS if d < TODAY), default=None)
    if last_real:
        for tc in TC_KEYS:
            cap = sum(actual_data[tc][sg].get(last_real, 0) for sg in actual_data[tc])
            if cap <= 0: continue
            for d in rem_days:
                cur = sum(proj_data[tc][sg].get(d, 0) for sg in proj_data[tc])
                if cur > cap: _apply_day_target(tc, d, cap)
        print(f'  CAP todos dias restantes <= ultimo realizado ({last_real})')

# 2f. Clampa projecao diaria NEGATIVA a 0 (o hazard organico pode gerar incremento
# negativo em alguns dias -> aparecia -8,5k no Micro). Piso em 0.
for tc in TC_KEYS:
    for sg in proj_data[tc]:
        for d in list(proj_data[tc][sg]):
            if proj_data[tc][sg][d] < 0:
                proj_data[tc][sg][d] = 0

# ============================================================
# 3. HISTORICO (snapshot LOCAL — nao toca producao)
# ============================================================
historico = {}
if HIST_FILE.exists():
    historico = json.loads(HIST_FILE.read_text(encoding='utf-8'))
if HIST_LOCAL.exists():
    historico.update(json.loads(HIST_LOCAL.read_text(encoding='utf-8')))
# adiciona o snapshot de hoje em memoria + salva so no arquivo local de dev
snap_today = {tc: {sg: dict(proj_data[tc][sg]) for sg in proj_data[tc]} for tc in TC_KEYS}
historico[TODAY] = snap_today
local_snaps = json.loads(HIST_LOCAL.read_text(encoding='utf-8')) if HIST_LOCAL.exists() else {}
local_snaps[TODAY] = snap_today
HIST_LOCAL.write_text(json.dumps(local_snaps, ensure_ascii=False, indent=2), encoding='utf-8')

def build_months_hist_entry(historico, ym_filter):
    snaps = [d for d in sorted(historico.keys(), reverse=True) if d not in SKIP_HIST_SNAPSHOTS]
    out = {tc: {} for tc in TC_KEYS}
    yr, mo = int(ym_filter[:4]), int(ym_filter[5:7])
    _, nd = monthrange(yr, mo)
    days = [date(yr, mo, d).isoformat() for d in range(1, nd + 1)]
    for tc in TC_KEYS:
        for D in days:
            for sd in snaps:
                if sd > D: continue
                snap = historico.get(sd, {})
                tot = sum(snap.get(tc, {}).get(sg, {}).get(D, 0) for sg in snap.get(tc, {}))
                if tot > 0:
                    out[tc][D] = int(round(tot)); break
    return out

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
    kpis[tc] = dict(real_total=int(real_total), proj_total=int(proj_total),
                    grand_total=int(real_total + proj_total), hoje_proj=int(hoje_proj),
                    pct_real=round(real_total / max(real_total + proj_total, 1) * 100, 1))

# ============================================================
# 5. MONTHS_* (mesma estrutura do front)
#   Safra de 6 meses: VIGENTE (index 0) + 5 FECHADOS anteriores.
#   - Mes vigente: PROJ ativa, todayStr = HOJE, closed=False, HIST reescalado (5b).
#   - Meses fechados: PROJ vazia, todayStr = 1o dia do mes seguinte (tudo vira
#     barra realizada, sem linha "HOJE"), closed=True, HIST CRU (projecao logada).
#     Jan-Mai nao tem snapshot -> HIST vazio (so realizado, sem linha laranja).
# ============================================================
def next_month_first(yr, mo):
    return (date(yr + 1, 1, 1) if mo == 12 else date(yr, mo + 1, 1)).isoformat()

# (key, yr, mo, is_current) — 6 meses, index 0 = vigente
target_months, yy, mm = [], year, month
for i in range(6):
    target_months.append((f'{yy}-{mm:02d}', yy, mm, i == 0))
    mm -= 1
    if mm == 0: mm = 12; yy -= 1

def meta_for(key, yr, mo, today_str, closed):
    _, nd = monthrange(yr, mo)
    days = [date(yr, mo, d).isoformat() for d in range(1, nd + 1)]
    labels = [f"{d[8:10]}/{MONTH_ABBR[mo]}" for d in days]
    return dict(label=f'{MONTH_ABBR[mo]}/{yr}', allDays=days, labels=labels,
                todayStr=today_str, closed=closed)

def slice_actuals(prefix):
    out = {}
    for tc in TC_KEYS:
        out[tc] = {}
        for sg in actual_data[tc]:
            d = {dia: v for dia, v in actual_data[tc][sg].items() if dia.startswith(prefix)}
            if d: out[tc][sg] = d
    return out

# 1o mes com projecao logada -> so a partir dele (vigente ou fechado) a linha
# laranja / o desvio Proj x Real aparecem. Meses fechados ANTERIORES = so realizado
# diario (sem linha de projecao), mesmo que existam snapshots antigos no historico.
FIRST_PROJ_MONTH = '2026-06'

# Junho/26 SO: a projecao logada (linha laranja) so comeca no dia 03 -> os dias
# 01 e 02 herdam o valor do dia 03, apenas para popular a linha. Regra one-off:
# meses futuros ja terao projecao desde o dia 1.
JUN_FILL_KEY = '2026-06'
def apply_jun_fill(hist_entry):
    for tc in TC_KEYS:
        hd = hist_entry.get(tc, {})
        v3 = hd.get('2026-06-03')
        if v3:
            hd.setdefault('2026-06-01', v3)
            hd.setdefault('2026-06-02', v3)

months_meta, months_actual, months_proj, months_hist = {}, {}, {}, {}
for key, yr, mo, is_cur in target_months:
    months_meta[key]   = meta_for(key, yr, mo, TODAY if is_cur else next_month_first(yr, mo), not is_cur)
    months_actual[key] = slice_actuals(key)
    months_proj[key]   = ({tc: {sg: dict(proj_data[tc][sg]) for sg in proj_data[tc]} for tc in TC_KEYS}
                          if is_cur else {tc: {sg: {} for sg in SG_ORDER} for tc in TC_KEYS})
    months_hist[key]   = (build_months_hist_entry(historico, key)
                          if (is_cur or key >= FIRST_PROJ_MONTH)
                          else {tc: {} for tc in TC_KEYS})

# 5b. REMOVIDO (2026-07-26, a pedido do usuario): antes a linha laranja do mes VIGENTE
#     nos dias passados era sobrescrita pelo REALIZADO -> erro 0 no fim do mes, impossivel
#     validar a projecao. Agora o mes vigente mantem a PROJECAO LOGADA (build_months_hist_entry,
#     dos snapshots) nos dias passados -> a linha revela o desvio vs as barras realizadas.
#     Dias futuros seguem usando PROJ (adaptam pelas regras). Meses fechados ja eram assim.

# 5c. fill one-off de junho (dias 01-02 = dia 03), em qualquer estado (vigente ou fechado)
if JUN_FILL_KEY in months_hist:
    apply_jun_fill(months_hist[JUN_FILL_KEY])

# ============================================================
# 6. SALVA _proj_data.json
# ============================================================
out = dict(generated=TODAY, active_month=cur_key, sg_order=SG_ORDER,
           META=months_meta, ACTUAL=months_actual, PROJ=months_proj, HIST=months_hist, KPIS=kpis)
OUT_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
print(f"\n_proj_data.json gerado ({OUT_FILE.stat().st_size/1024:.0f} KB)")
print(f"Resumo {TODAY}:")
for tc in TC_KEYS:
    k = kpis[tc]
    print(f"  {tc:9s}  real={k['real_total']:>10,}  proj={k['proj_total']:>10,}  total={k['grand_total']:>10,}".replace(',', '.'))
