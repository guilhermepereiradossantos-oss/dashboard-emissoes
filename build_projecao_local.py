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
# 2026-08-27: ver nota em push_datasets_to_grid.py — ...725... trava por quota (jobs em PENDING
# eterno); ...341... e o padrao estavel.
PROJECT_ID = 'ddme000341-ox7qb27ldi8-furyid'
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
# 2026-08-17 (3): TARGET OFF de novo. O problema real nao era so o total, era o SHAPE: o template
#   replicava o nivel SUSTENTADO de julho (~24k/dia), irreal p/ agosto pos-pico. O realizado mostra
#   o pico ja passou (Full 40k/36k em 14-15 -> ja ~14k em 16). Trava de total sobre um shape errado
#   so mascarava. Solucao no bloco 2i: cauda = run-rate recente DECAINDO (ancora no dado real).
#   Isso derruba o total p/ ABAIXO de julho naturalmente, sem precisar de valor fixo.
#   2026-08-17 (4): Full fica ~419k (-16% vs jul) so com a cauda 2i — abaixo de julho, otimo.
#   MICRO com a cauda da ~152k (+6% vs jul): ja converteu 89k ate o dia 17, entao nem decaindo
#   fecha abaixo de julho sozinho. Decisao do usuario = abaixo de julho nos DOIS -> travo SO o
#   Micro em 136.389 (o 2b escala a cauda ja decaida). Full segue livre (sem trava). *** PONTUAL. ***
#   2026-08-21: AMBAS AS TRAVAS DE AGOSTO DESLIGADAS (pedido do usuario: "essa projecao agora esta
#   alta pelo que vem sendo realizado, ajuste Full e revise Micro"). As travas estavam empurrando os
#   dois lados para o valor ERRADO, em direcoes OPOSTAS:
#     - TC Full travado em 450.000 forcava f=1,0062 sobre uma cauda que ja vinha inflada pela ancora
#       velha (mediana 7d contaminada pelo pico do lote) -> cauda comecava em 16,4k/d, ACIMA do
#       ultimo dia real (12,8k). Com a cauda v2 calibrada, o Full cai pro nivel que o proprio
#       decaimento historico indica, sem valor fixo.
#     - Micro TC travado em 136.389 forcava f=0,6132, ou seja, CORTAVA a projecao a ~2,7k/dia quando
#       os ultimos 6 dias completos rodaram a ~5,6k/dia (media). Era a trava, nao o modelo, mandando.
#       A trava veio de uma decisao de 17/08 ("abaixo de julho nos dois"); com o realizado de agosto
#       essa premissa nao se sustenta mais no Micro -> devolvido ao organico. *** Se voce quiser
#       manter Micro abaixo de julho por decisao de negocio, e so repor a trava aqui. ***
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
# 2b. ESCALA PRA TARGET — MOVIDA p/ depois do clamp 2f (e do 2g).
#   Bug corrigido 2026-08-04: rodando aqui (antes do 2f), a escala tocava tambem os dias
#   com projecao NEGATIVA; o 2f depois zerava esses dias e RE-INFLAVA o total acima do alvo
#   (ex.: alvo 454.437 virava 479.373). Agora o TARGET e o ULTIMO ajuste sobre proj_data,
#   aplicado sobre valores ja clampados -> o total bate o alvo exatamente. Ver bloco pos-2g.

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
# 2g. AJUSTE PONTUAL (2026-07-27, info do usuario) — *** NAO RECORRENTE ***
#   Encendido de SELLERS previsto p/ ~28/07: Full 420k + Micro 78k. Faltam ~5 dias -> soma
#   as conversoes esperadas (curva seller recente, cumulativa D0..D3) aos dias restantes.
#   D0 domina (EA de seller + rapidos). *** REMOVER apos julho/26 OU quando o encendido real
#   entrar na base *** (senao conta dobrado). Curva jun-jul/26: Full 1.92/2.44/2.83/3.18 ;
#   Micro 6.15/8.26/9.06/9.58 (cumulativo %). Se a data mudar, ajustar 'data_enc'.
# DESATIVADO 2026-07-28: o encendido real de seller ENTROU NA BASE (27-28/07: Full ~378k,
# Micro ~65k) -> o modelo nativo (proj_organico) ja captura. Reativar contaria DOBRADO.
SELLER_ENC_ONEOFF = None
if SELLER_ENC_ONEOFF and SELLER_ENC_ONEOFF['data_enc'][:7] == cur_key:
    _d0 = date.fromisoformat(SELLER_ENC_ONEOFF['data_enc'])
    for tc in TC_KEYS:
        _cfg = SELLER_ENC_ONEOFF[tc]
        _add = 0.0
        for i, frac in enumerate(_cfg['inc']):
            dd = (_d0 + timedelta(days=i)).isoformat()
            if dd[:7] != cur_key or dd < TODAY:
                continue
            proj_data[tc]['Sellers'][dd] += _cfg['n'] * frac
            _add += _cfg['n'] * frac
        print(f"  [ONE-OFF seller enc] {tc}: +{int(round(_add)):,}".replace(',', '.'))

# janela = 6 meses (vigente + 5 anteriores), igual a safra do dash
_ey, _em = year, month
for _ in range(5):
    _em -= 1
    if _em == 0: _em = 12; _ey -= 1
_enc_ini = f'{_ey}-{_em:02d}'

# ============================================================
# 1b. DATA DO ENCENDIDO (lote) por mes e por TC  [2026-08-21]
#   (fica ANTES do 2i porque a cauda pos-lote usa esta data como referencia)
#   Usado pelos graficos "Comparativo com meses anteriores" (faixa historica + acumulado):
#   marca o dia do lote como referencia. O encendido e MUITO concentrado (medido: 51-92% do
#   mes num unico dia), entao "o dia do lote" = dia de maior volume de DT_ENCENDIDO.
#   NAO-FATAL: se a query falhar, o bundle sai sem ENCENDIDO e o front simplesmente nao
#   desenha os marcadores (nunca derruba o push por causa disso).
# ============================================================
# ════════════════════════════════════════════════════════════════════════════════
# MES PRE-LOTE (2026-09-04)
# ════════════════════════════════════════════════════════════════════════════════
# Volume minimo p/ um dia contar como LOTE. Os lotes reais sao de milhoes; o ruido
# diario de um mes pre-lote fica na casa de 1-4 mil.
LOTE_MIN_QTDE = 100_000
# Data prevista do lote, informada pelo negocio (o modelo nao tem como saber). Serve p/
# delimitar ate onde vai a ancoragem no realizado; do lote em diante vale o shape organico.
# Deixar o mes de fora = o codigo usa o dia de PICO da propria projecao organica.
LOTE_PREVISTO = {'2026-09': '2026-09-15'}   # usuario em 04/09: encendido entre 15 e 16/09
# ── DATA DUPLA (2026-09-04) ──
# Medido nas datas duplas de 2026 com o lote longe (o lote contamina os 4 dias seguintes):
#   02/02 +11,8% · 04/04 +1,7% · 06/06 -0,2% · 07/07 +36,8% · 08/08 +12,9%
#   media +12,6%, mediana +11,8% (num dia tipico de 2026 a mediana e +0,4%)
# Efeito real mas IRREGULAR (2 das 5 nao mostraram nada), e vale por UM dia so: ~1,3k cartoes
# num mes de ~400k. Fica explicito e facil de desligar (basta esvaziar o dict).
DATA_DUPLA_UPLIFT = 0.12
DATA_DUPLA_DIAS = {'2026-09': '2026-09-09'}

ENC_SQL = f"""
SELECT FORMAT_DATE('%Y-%m', DT_ENCENDIDO) AS ym,
       FORMAT_DATE('%Y-%m-%d', DT_ENCENDIDO) AS dia,
       CASE WHEN FLAG_TC LIKE '%Full%' THEN 'TC Full' ELSE 'Micro TC' END AS tc,
       SUM(QTDE) AS enc
FROM `meli-bi-data.SBOX_CREDITSTC.base_projecao_Gui`
WHERE DT_ENCENDIDO >= '{_enc_ini}-01'
  AND DT_ENCENDIDO <  DATE_ADD(DATE_TRUNC(CURRENT_DATE(), MONTH), INTERVAL 1 MONTH)
GROUP BY 1, 2, 3
"""
encendido = {}
try:
    print("Rodando query de datas de encendido (lote)...")
    # Passa via STDIN (mesmo padrao da query principal). Passar SQL multi-linha como
    # argumento com shell=True no Windows quebra (o shell mastiga backticks/%/quebras).
    _enc_file = REPO / '_enc_dates.sql'
    _enc_file.write_bytes(ENC_SQL.encode('utf-8'))   # bytes = sem BOM (BOM da erro \357 no bq)
    with open(_enc_file, 'rb') as _f:
        _r = subprocess.run(
            [BQ_CMD, 'query', f'--project_id={PROJECT_ID}', '--use_legacy_sql=false',
             '--nouse_cache', '--format=csv', '--max_rows=10000'],
            stdin=_f, capture_output=True, env=env, shell=True)
    if _r.returncode != 0:
        raise RuntimeError((_r.stderr.decode(errors='replace') or 'sem stderr')[:300])
    _per = defaultdict(lambda: defaultdict(float))   # (tc)(dia)
    for _row in csv.DictReader(io.StringIO(_r.stdout.decode('utf-8', errors='replace'))):
        if not _row.get('ym'):
            continue
        _v = float(_row['enc'] or 0)
        _per[_row['tc']][_row['dia']] += _v
        _per['Total'][_row['dia']]    += _v
    for _tc, _days in _per.items():
        _bym = defaultdict(list)
        for _d, _v in _days.items():
            _bym[_d[:7]].append((_d, _v))
        encendido[_tc] = {}
        for _ym, _lst in _bym.items():
            _lst.sort(key=lambda x: -x[1])
            _tot = sum(v for _, v in _lst) or 1
            # 2026-09-04: so vale como LOTE se o dia for material. Num mes PRE-LOTE o "dia de
            # maior encendido" e ruido: em 2026-09 saiu 01/09 com 1.634 encendidos, e a cauda
            # passou a contar D+ a partir dessa data falsa -> achatou o mes inteiro e apagou o
            # pico do lote que ainda vai acontecer (setembro caiu p/ 264k, contra ~445k).
            if _lst[0][1] < LOTE_MIN_QTDE:
                continue                      # mes sem lote (ainda) -> nao entra em `encendido`
            encendido[_tc][_ym] = dict(dia=_lst[0][0], qtde=int(round(_lst[0][1])),
                                       share=round(_lst[0][1] / _tot, 3))
    print(f"  -> encendido OK: " + ', '.join(
        f"{k}={v.get(cur_key, {}).get('dia', '?')}" for k, v in encendido.items()))
except Exception as _e:
    print(f"  [WARN] datas de encendido indisponiveis ({_e}); bundle sai sem ENCENDIDO")
    encendido = {}

# ============================================================
# 2i. CAUDA POS-LOTE CALIBRADA NO HISTORICO  (v2 - 2026-08-21)
#
#   POR QUE MUDOU: a v1 (17/08) ancorava a cauda na mediana dos ultimos 7 dias
#   (`recent_runrate(tc, 7)`). O usuario viu em 21/08 que a projecao estava "alta pelo que vem
#   sendo realizado". Dois defeitos:
#     (1) a janela de 7 dias AINDA CONTEM o pico do lote (14-15/08: 40k e 36k no Full) -> a
#         mediana saiu 16.333/d, ACIMA do ultimo dia real (12,3k). A cauda COMECAVA SUBINDO,
#         o oposto do comportamento pos-pico;
#     (2) dias com carga parcial na base entravam na ancora (o 20/08 chegou a aparecer com 358
#         no Full, ~3% do esperado, antes de a base completar).
#
#   COMO FUNCIONA AGORA (sem numero chumbado, tudo medido na hora):
#     a) ancora  = media dos ultimos 3 dias realizados COMPLETOS (`_complete_real_days`);
#     b) janelas = converte tudo p/ "D+n desde o lote" (data real do encendido, secao 1b). A
#        janela observada e a dos 3 dias do item (a); a janela a projetar e D+n do 1o dia futuro
#        ate o fim do mes. Isso importa porque cada produto esta num ponto diferente da curva:
#        em 21/08 o Full estava em D+6 (lote 15/08) e o Micro em D+9 (lote 12/08);
#     c) fator   = MEDIANA, entre os meses anteriores, de  media(janela_a_projetar)
#                                                        / media(janela_observada)
#        -> le o decaimento REAL da curva naquele trecho, por produto, em vez de supor;
#     d) total   = ancora * fator * n_dias;  distribui com decaimento geometrico r saindo do
#        ULTIMO dia real (continuidade visual: sem degrau entre realizado e projetado), com r
#        resolvido p/ fechar o total. r limitado a [0,85; 1,05] p/ nao gerar shape absurdo.
#   Mantem o shape organico "pico -> queda" que o usuario pediu (o pico ja e realizado; aqui
#   modela-se so a queda). NAO e run-rate achatado nem cap.
#
#   Fatores medidos em 21/08 (abr-jul/26): Full D+3..5 -> D+6..16 = 0,688 (spread 0,51-0,75);
#   Micro D+6..8 -> D+9..19 = 0,951 (spread 0,66-1,63; Micro converte mais espalhado, cauda
#   quase nao decai). Se o spread estiver largo, o valor e a mediana - trate como estimativa.
#
#   Roda p/ QUALQUER mes (nao e pontual de agosto): se faltar encendido ou historico na janela,
#   nao mexe e deixa a projecao organica passar.
# ============================================================
# Decaimento diario da tendencia (suave e FIXO) + fator historico limitado a 1,0.
#
# (2026-08-24) Antes eu fixava o inicio da cauda no nivel atual e RESOLVIA o r p/ fechar o total.
# Com um fator historico de 0,825 em 8 dias, isso exigia r=0,942 (-5,8%/dia) e a cauda caia ~34%
# de ponta a ponta -> a ultima segunda do mes ficava ABAIXO do sabado da semana anterior. Nao era
# bug de weekday (o perfil ja estava certo dentro de cada semana), era a inclinacao: em 8 dias o
# decaimento supera a diferenca util-vs-fds. Invertido: agora o DECAIMENTO e fixo e suave e o
# NIVEL e resolvido p/ fechar o total. Mesmo total, shape realista, sem inversao de calendario.
#
# Fator limitado a 1,0: cauda pos-lote e coorte se esgotando, o piso e "para de cair", nao "volta
# a subir". O historico do Micro deu 1,113 na janela atual (essa parte da cauda historicamente
# sobe), mas agosto vem caindo forte (18/08 6.091 -> 23/08 2.659); deixar subir contrariaria o
# dado recente e reintroduziria a inversao.
CAUDA_DECAY_DIA = 0.985
CAUDA_F_MAX = 1.00
# Janela do fator de weekday: so os ultimos ~90d. (2026-08-24) Com os 5 meses inteiros o sabado
# do TC Full saia em 0,95 (-7% vs dia util), mas o sabado REALIZADO de agosto foi bem mais fraco
# (22/08 = 10.677 vs nivel de dia util ~12,3k = -13%). O comportamento de fim de semana mudou;
# 90d segue o padrao atual em vez de diluir com abr/mai.
DOW_JANELA_DIAS = 90
# Tendencia da cauda: quando a serie des-sazonalizada JA ACHATOU, nao aplicar mais decaimento.
# (2026-08-24) Descoberta ao investigar o feedback do usuario ("ainda esta impactando demais os
# dias de semana"): o fator historico (0,77-0,83) foi medido em meses cuja curva AINDA ESTAVA
# CAINDO na janela de observacao (des-sazonalizado, abr 23,9k->12,9k de D+4 a D+8; mai
# 16,3k->11,8k; jul 16,5k->14,2k). Agosto ja chegou no piso NA propria janela (D+4 12,1k -> D+8
# 12,1k, plano). Aplicar "a queda continua" sobre uma serie que ja parou de cair e erro de
# premissa: descontava a queda duas vezes. Agora a TENDENCIA vem da propria serie recente
# (limitada a [0,97; 1,00] p/ nao extrapolar ruido), e o fator historico fica so como referencia
# no log.
CAUDA_R_OBS_MIN, CAUDA_R_OBS_MAX = 0.97, 1.00

def _complete_real_days(tc):
    """dias realizados do mes vigente, descartando dias do FIM com carga parcial na base."""
    daily = _daily_real(tc)
    ds = sorted(d for d in daily if d.startswith(cur_key) and daily[d] > 0)
    if len(ds) < 4:
        return [(d, daily[d]) for d in ds]
    ref = _median([daily[d] for d in ds])
    while len(ds) > 4 and daily[ds[-1]] < 0.35 * ref:
        print(f'    [carga parcial] {ds[-1]} = {int(daily[ds[-1]]):,} '
              f'({daily[ds[-1]] / ref * 100:.0f}% da mediana do mes) -> fora da ancora'.replace(',', '.'))
        ds.pop()
    return [(d, daily[d]) for d in ds]

def _month_daily(tc, ym):
    """serie diaria realizada de um mes fechado (para medir o fator historico)."""
    daily = _daily_real(tc)
    return {d: v for d, v in daily.items() if d.startswith(ym) and v > 0}

def _win_mean(serie, enc_iso, lo, hi):
    """media da serie na janela D+lo..D+hi desde o encendido; None se cobertura fraca."""
    if not enc_iso:
        return None
    e = date.fromisoformat(enc_iso)
    vs = [v for d, v in serie.items() if lo <= (date.fromisoformat(d) - e).days <= hi]
    return sum(vs) / len(vs) if len(vs) >= 2 else None

def _hist_tail_factor(tc, ow, pw):
    """mediana, nos meses anteriores, de media(janela pw) / media(janela ow)."""
    fs = []
    for ym in sorted(set(d[:7] for d in _daily_real(tc)) - {cur_key}):
        enc = (encendido.get(tc, {}).get(ym) or {}).get('dia')
        serie = _month_daily(tc, ym)
        o = _win_mean(serie, enc, *ow)
        p = _win_mean(serie, enc, *pw)
        if o and p and o > 0:
            fs.append(p / o)
    return (_median(fs), len(fs)) if fs else (None, 0)

def _dow_factors_clean(tc):
    """Fator por dia-da-semana (0=Seg..6=Dom), normalizado p/ media 1.

    Pedido do usuario (2026-08-24): "e bem improvavel que vamos fazer menos emissoes na
    semana do que no final de semana" — a cauda era decaimento PURO, entao um sabado no
    inicio dela ficava acima de uma segunda no fim. Aqui mede-se o padrao semanal real.

    Metodo: cada dia entra como RAZAO sobre a MEDIANA MOVEL CENTRADA de 7 dias (3 antes,
    3 depois). Isso remove de uma vez o nivel do mes E a tendencia local (a cauda do lote),
    sobrando so o efeito de dia-da-semana. Mediana (nao media) na janela p/ um pico de lote
    dentro dela nao contaminar a base. Ainda exclui D+0..D+1 do lote (o proprio spike, cuja
    razao seria absurda).

    Por que nao "razao sobre a media do mes" (1a tentativa, 24/08): p/ lote que cai numa
    QUARTA (10/06 e 12/08 no Micro), excluir D+0..D+2 tira qua/qui/sex mas deixa entrar o
    SABADO seguinte (D+3) ainda inflado pela cauda -> o fator de sabado subia artificialmente.
    No Micro isso dava Sab=0,944 vs Sex=0,935, ou seja "sabado >= sexta", o oposto do real
    (em agosto: sexta 21 = 2.868 vs sabado 22 = 2.323, -19%). Com a mediana movel, o mesmo
    calculo da Sab -5,2% vs Sex, coerente.
    """
    daily = _daily_real(tc)
    spike = set()
    for _ym, _info in (encendido.get(tc) or {}).items():
        _e0 = date.fromisoformat(_info['dia'])
        for _k in range(0, 2):
            spike.add((_e0 + timedelta(days=_k)).isoformat())
    _cut = (date.today() - timedelta(days=DOW_JANELA_DIAS)).isoformat()
    ks = sorted(d for d, v in daily.items() if v > 0 and d >= _cut)
    byw = defaultdict(list)
    for d in ks:
        if d in spike:
            continue
        dt = date.fromisoformat(d)
        win = [daily[x] for x in ks if abs((date.fromisoformat(x) - dt).days) <= 3]
        if len(win) < 5:
            continue
        base = _median(win)
        if base > 0:
            byw[dt.weekday()].append(daily[d] / base)
    med = {wd: _median(vs) for wd, vs in byw.items() if len(vs) >= 5}
    if len(med) < 7:
        return {}
    mean_med = sum(med.values()) / len(med)
    return {wd: med[wd] / mean_med for wd in med} if mean_med > 0 else {}

_rem = sorted(d for d in ALL_DAYS if d >= TODAY)

# ============================================================
# 2i-PRE. MES PRE-LOTE (2026-09-04) — ancora os dias ATE o lote no realizado.
#
# Situacao: comeco de mes, o lote de encendido ainda nao rodou. O organico projeta os
# primeiros dias replicando o mes anterior e sai ACIMA do que esta acontecendo (em 03/09
# projetava 13,2k/dia com o realizado do dia 02 em 11,0k, e subindo daí).
# A cauda (2i) NAO serve aqui: ela existe p/ depois do lote e, sem lote no mes, contava D+
# a partir de um dia falso -> em 04/09 achatou setembro inteiro p/ 264k, apagando o pico.
#
# Regra: do dia de hoje ate a VESPERA do lote, o nivel vem do realizado (media
# des-sazonalizada dos 3 ultimos dias completos, com a tendencia da propria serie).
# Do lote em diante, PRESERVA o organico — e ele que carrega o pico da safra nova.
# ============================================================
_sem_lote_no_mes = not any((encendido.get(tc, {}) or {}).get(cur_key) for tc in TC_KEYS)
if _rem and _sem_lote_no_mes:
    _lote_prev = LOTE_PREVISTO.get(cur_key)
    for tc in TC_KEYS:
        _comp = _complete_real_days(tc)
        if len(_comp) < 3:
            print(f'  [PRE-LOTE {tc}] so {len(_comp)} dia(s) completo(s) -> organico'); continue
        _diaria = defaultdict(float)
        for sg in proj_data[tc]:
            for d, v in proj_data[tc][sg].items():
                if d.startswith(cur_key): _diaria[d] += v
        _pico = max(_diaria, key=lambda d: _diaria[d]) if _diaria else None
        _corte = _lote_prev or _pico
        if not _corte:
            print(f'  [PRE-LOTE {tc}] sem data de lote e sem pico organico -> organico'); continue
        if _pico and _lote_prev:
            _dif = (date.fromisoformat(_pico) - date.fromisoformat(_lote_prev)).days
            if abs(_dif) > 2:
                # Nao desloco a curva automaticamente: mover uma coorte inteira e arriscado e
                # nao da p/ validar sem um caso real. Fica o aviso p/ decisao humana.
                print(f'    [ATENCAO {tc}] pico do organico em {_pico} vs lote informado '
                      f'{_lote_prev} ({_dif:+d}d) -> shape possivelmente desalinhado')
        _alvo = [d for d in _rem if d < _corte]
        if not _alvo:
            print(f'  [PRE-LOTE {tc}] lote {_corte} e hoje ou ja passou -> organico'); continue
        _dow = _dow_factors_clean(tc)
        _wf = (lambda iso: _dow.get(date.fromisoformat(iso).weekday(), 1.0)) if _dow else (lambda iso: 1.0)
        _obs = _comp[-3:]
        _lvl = sum(v / _wf(d) for d, v in _obs) / len(_obs)
        _dom = lambda iso: int(iso[8:10])
        _ds = [(d, v / _wf(d)) for d, v in _comp]
        _r_obs = None
        if len(_ds) >= 4:
            _h = len(_ds) // 2
            _A, _B = _ds[:_h], _ds[_h:]
            _ma = sum(v for _, v in _A) / len(_A); _mb = sum(v for _, v in _B) / len(_B)
            _ga = sum(_dom(d) for d, _ in _A) / len(_A); _gb = sum(_dom(d) for d, _ in _B) / len(_B)
            if _ma > 0 and _gb > _ga:
                _r_obs = (_mb / _ma) ** (1.0 / (_gb - _ga))
        # Mesmos limites da cauda: a serie pre-lote e o rabo da coorte anterior, entao ela
        # nao volta a subir sozinha; sem serie suficiente, fica PLANA (r=1) em vez de chutar.
        _r = min(max(_r_obs, CAUDA_R_OBS_MIN), CAUDA_R_OBS_MAX) if _r_obs else 1.0
        for i, d in enumerate(_alvo):
            _apply_day_target(tc, d, _lvl * (_r ** (i + 1)) * _wf(d))
        _soma = sum(proj_data[tc][sg].get(d, 0) for sg in proj_data[tc] for d in _alvo)
        print(f'  [PRE-LOTE {tc}] lote previsto {_corte} (pico organico {_pico}) | '
              f'nivel-base={int(_lvl):,}/d | r={_r:.4f}'.replace(',', '.')
              + f' -> {len(_alvo)} dias ancorados = {int(_soma):,}'.replace(',', '.'))

if _rem and not _sem_lote_no_mes:
    for tc in TC_KEYS:
        _comp = _complete_real_days(tc)
        _enc = (encendido.get(tc, {}).get(cur_key) or {}).get('dia')
        if len(_comp) < 3 or not _enc:
            print(f'  [CAUDA {tc}] sem ancora (dias completos={len(_comp)}, lote={_enc}) -> organico')
            continue
        _e = date.fromisoformat(_enc)
        _dp = lambda iso: (date.fromisoformat(iso) - _e).days
        _obs_days = _comp[-3:]
        _ow = (_dp(_obs_days[0][0]), _dp(_obs_days[-1][0]))      # janela observada, em D+
        _pw = (_dp(_rem[0]), _dp(_rem[-1]))                      # janela a projetar, em D+
        # 2026-08-31: o fator historico virou SO REFERENCIA DE LOG em 24/08 (a tendencia passou a
        # sair da propria serie recente, _r_obs). O `continue` aqui era resto da versao antiga e
        # abortava a cauda INTEIRA por um valor que o calculo nem usa mais -> caia no organico cru,
        # sem ancora no realizado. Isso acontecia em TODO ultimo dia do mes: com 1 dia restante a
        # janela a projetar tem tamanho 1 (D+n..D+n) e o _win_mean exige >= 2 dias, entao o fator
        # SEMPRE saia None. Efeito medido em 31/08: Full projetava 16.069 p/ uma segunda cuja media
        # dos 7 dias anteriores era 10.429 (+54%); Micro 5.024 vs 2.650 (+90%) -- justo no dia em que
        # o numero e lido como "o fechado do mes". Agora o fator ausente so tira a linha de
        # referencia do log; a cauda segue ancorada no nivel-base realizado.
        _f, _nm = _hist_tail_factor(tc, _ow, _pw)
        if not _f:
            print(f'  [CAUDA {tc}] sem fator historico na janela D+{_pw[0]}..D+{_pw[1]} '
                  f'(so referencia de log) -> segue com ancora no realizado')
        # ---- sazonalidade semanal (2026-08-24) ----
        # A cauda era decaimento PURO: um sabado no inicio dela saia acima de uma segunda no
        # fim, o que nao acontece na pratica. Agora o decaimento define a TENDENCIA e o fator
        # de weekday define o PERFIL dentro da semana.
        _dow = _dow_factors_clean(tc)
        _wf = (lambda iso: _dow.get(date.fromisoformat(iso).weekday(), 1.0)) if _dow else (lambda iso: 1.0)
        # DES-sazonaliza a ancora: se o ultimo dia real caiu num dia forte/fraco, o nivel-base
        # nao pode herdar isso (senao o efeito weekday entra duas vezes).
        # Nivel-base = MEDIA des-sazonalizada dos 3 dias (nao so o ultimo dia). Usar so o
        # ultimo dia fazia o resultado depender de em que weekday ele caiu: em 24/08 o ultimo
        # real era um DOMINGO e, des-sazonalizado, saia +11% acima da media (Micro) -> a cauda
        # comecava alta e o r tinha que ficar mais ingreme p/ fechar o total, o que jogava um
        # dia util do fim do mes abaixo de um fim de semana do inicio. A media dos 3 dias e
        # estavel e faz o r refletir a TENDENCIA, nao o calendario do ultimo dia.
        # nivel-base = media des-sazonalizada dos 3 ultimos dias completos
        _lvl = sum(v / _wf(d) for d, v in _obs_days) / len(_obs_days)
        # TENDENCIA da propria serie, medida SO nos dias ja assentados (D+4 em diante).
        # Se a serie ja achatou, sai ~1,0 e a cauda fica plana (caso de agosto/26); se ainda
        # esta caindo, sai <1 e a cauda decai.
        # *** Cuidado que custou uma iteracao (24/08): usar "3 ultimos vs 3 anteriores" sobre
        # TODOS os dias pegava o D+3 (18/08 = 16.333, ainda inflado pelo lote) como base do
        # "antes" -> a tendencia saia 0,975 (queda de 2,5%/dia) quando os dias assentados
        # estavam PLANOS. Isso e a queda do pico, nao a tendencia da cauda. Cortando em D+4 o
        # mesmo calculo da ~1,00 no Full. ***
        _ds = [(dd, vv / _wf(dd)) for dd, vv in _comp if _dp(dd) >= 4]
        _r_obs = None
        if len(_ds) >= 4:
            _h = len(_ds) // 2
            _A, _B = _ds[:_h], _ds[_h:]
            _ma = sum(v for _, v in _A) / len(_A)
            _mb = sum(v for _, v in _B) / len(_B)
            _ga = sum(_dp(dd) for dd, _ in _A) / len(_A)       # midpoint em D+ de cada metade
            _gb = sum(_dp(dd) for dd, _ in _B) / len(_B)
            if _ma > 0 and _gb > _ga:
                _r_obs = (_mb / _ma) ** (1.0 / (_gb - _ga))    # taxa por dia
        _r = min(max(_r_obs, CAUDA_R_OBS_MIN), CAUDA_R_OBS_MAX) if _r_obs else CAUDA_DECAY_DIA
        _n = len(_rem)
        _wl = [_wf(d) for d in _rem]
        for i, d in enumerate(_rem):
            _apply_day_target(tc, d, _lvl * (_r ** i) * _wl[i])
        _tail = sum(proj_data[tc][sg].get(d, 0) for sg in proj_data[tc] for d in _rem)
        _WDN = ['Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sab', 'Dom']
        print(f'  [CAUDA {tc}] lote={_enc} | obs D+{_ow[0]}..D+{_ow[1]} nivel-base={int(_lvl):,}/d '
              f'| tendencia obs r={_r:.4f}' + (f' (crua {_r_obs:.4f})' if _r_obs else ' (sem serie -> default)')
              + (f' | ref fator hist={_f:.3f} ({_nm}m)' if _f else ' | ref fator hist=n/d')
              + f' -> cauda {_n}d = {int(_tail):,}'.replace(',', '.'))
        if _dow:
            print('    DOW ' + ' '.join(f'{_WDN[w]}={_dow[w]:.2f}' for w in sorted(_dow)))
        else:
            print('    [WARN] sem fator de weekday (historico insuficiente) -> cauda sem perfil semanal')

# ============================================================
# 2k. DATA DUPLA (2026-09-04, pedido do usuario) — uplift de UM dia.
#   Medido nas datas duplas de 2026 com o lote longe: media +12,6%, mediana +11,8%
#   (num dia tipico de 2026 o mesmo indicador da +0,4%). Efeito real mas irregular:
#   02/02 +11,8% · 04/04 +1,7% · 06/06 -0,2% · 07/07 +36,8% · 08/08 +12,9%.
#   Vale por UM dia: ~1,3k cartoes num mes de ~400k, ou seja, NAO muda o fechamento.
#   Aplicado so se o dia ainda for projecao (se ja virou realizado, nao se mexe).
#   Desligar = esvaziar DATA_DUPLA_DIAS.
# ============================================================
_dd = DATA_DUPLA_DIAS.get(cur_key)
if _dd and _dd >= TODAY:
    for tc in TC_KEYS:
        _antes = sum(proj_data[tc][sg].get(_dd, 0) for sg in proj_data[tc])
        if _antes <= 0:
            continue
        for sg in proj_data[tc]:
            if _dd in proj_data[tc][sg]:
                proj_data[tc][sg][_dd] *= (1 + DATA_DUPLA_UPLIFT)
        _dep = sum(proj_data[tc][sg].get(_dd, 0) for sg in proj_data[tc])
        print(f'  [DATA DUPLA {tc}] {_dd}: {int(_antes):,} -> {int(_dep):,} '
              f'(+{DATA_DUPLA_UPLIFT*100:.0f}%, +{int(_dep-_antes):,} cartoes)'.replace(',', '.'))
elif _dd:
    print(f'  [DATA DUPLA] {_dd} ja e realizado -> sem ajuste')

# ============================================================
# 2b (reposicionado). ESCALA PRA TARGET — ULTIMO ajuste sobre proj_data.
#   Aplicado DEPOIS do clamp 2f (sem negativos) e do 2g, entao o total do mes vigente
#   bate o alvo exatamente. Escala a curva organica remanescente preservando o shape.
# ============================================================
# 2h. REMOVIDO (2026-08-17) — era o blend que atenuava o pico de TC Full em ago (pontual de
#   quando o pico era projetado/artificial). Com o batch ja realizado e o TARGET desligado, a
#   projecao volta ao ORGANICO padrao (sem blend). Ver comentario do TARGET_TOTALS.

_tgt_cfg = TARGET_TOTALS.get(cur_key)
if _tgt_cfg:
    for tc in TC_KEYS:
        target = _tgt_cfg.get(tc)
        if not target: continue
        real_so_far = sum(v for sg in actual_data[tc] for d, v in actual_data[tc][sg].items() if d.startswith(cur_key))
        proj_raw = sum(v for sg in proj_data[tc] for d, v in proj_data[tc][sg].items() if d.startswith(cur_key))
        proj_target = target - real_so_far
        if proj_raw > 0 and proj_target > 0:
            factor = proj_target / proj_raw
            for sg in proj_data[tc]:
                for d in list(proj_data[tc][sg].keys()):
                    if d.startswith(cur_key): proj_data[tc][sg][d] *= factor
            print(f'  TARGET {tc}: real={int(real_so_far):,} + proj_raw={int(proj_raw):,} -> alvo_proj={int(proj_target):,} (f={factor:.4f}) -> total={int(target):,}')
        elif proj_target <= 0:
            for sg in proj_data[tc]:
                for d in list(proj_data[tc][sg].keys()):
                    if d.startswith(cur_key): proj_data[tc][sg][d] = 0
            print(f'  TARGET {tc}: real>=target; proj zerada')

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

# 5d. REMOVIDO DE NOVO (2026-08-12, a pedido enfatico do usuario). NAO sobrescrever a linha
#     laranja do mes vigente com o REALIZADO dos dias passados. A linha deve manter a PROJECAO
#     LOGADA (build_months_hist_entry, dos snapshots) tambem nos dias passados, p/ revelar o
#     desvio vs as barras realizadas. Mesma decisao de 2026-07-26. *** NAO reintroduzir. ***
#     (O 08-06 tinha reintroduzido isso p/ matar um 8k defasado; a solucao correta NAO e copiar
#     o realizado — se um snapshot antigo ficar defasado, usar SKIP_HIST_SNAPSHOTS, nao overlay.)

# ============================================================
# 6. SALVA _proj_data.json
# ============================================================
out = dict(generated=TODAY, active_month=cur_key, sg_order=SG_ORDER,
           META=months_meta, ACTUAL=months_actual, PROJ=months_proj, HIST=months_hist,
           KPIS=kpis, ENCENDIDO=encendido)
OUT_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
print(f"\n_proj_data.json gerado ({OUT_FILE.stat().st_size/1024:.0f} KB)")
print(f"Resumo {TODAY}:")
for tc in TC_KEYS:
    k = kpis[tc]
    print(f"  {tc:9s}  real={k['real_total']:>10,}  proj={k['proj_total']:>10,}  total={k['grand_total']:>10,}".replace(',', '.'))
