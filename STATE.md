# Dashboard Emissoes & Encendidos — Estado Atual

> Documento vivo. Atualizar a cada mudanca relevante (UI, dados, processo).
> URL publicada: https://guilhermepereiradossantos-oss.github.io/dashboard-emissoes/

---

## 1. Visao geral

Dashboard dark-mode com 3 abas que compartilham um seletor de mes (botoes Abr/2026 / Mai/2026):

### Aba **Projeção TCMP** (id `tab-projecao`; antiga "Emissoes" — montada via Claude Code — `run_dashboard.py` original)
- KPIs TC Full e Micro TC: Realizado, Projecao hoje, Restante, Total Estimado, Plano vs MoM
- Grafico stacked diario por super_grupo + linha de projecao
- Filtros: super_grupos (BAU, EA, Sellers, Cuentas Canceladas, Only Nav, Mar Aberto)
- Adoption section: tabela 30d por TC
- Grand Total
- **Fatores de Projecao** (dinamico via `renderFatores`)

### Aba **Emissões** (id `tab-emissoes`; convertidos via DT_CONV + janela 30d — adicionada 28/05/2026)
- Toggle TC Full / Micro TC
- KPI panels (Mes Atual vs Mes Anterior): 2 cards cada (Emissões nominal + % Adoption = emi/enc)
- **Evolucao por Safra**: barra unica Emissões/mes (8 meses, single-color verde) + tabela transposta (Encendidos / Emissões / % Adoption)
- **Evolucao Diaria**: barras Emissões/dia (Abr/26 + Mai/26 cobertos — outros meses caem em placeholder)
- **Share por Safra**: 4 graficos 100%-stacked (NISE / App Ativo / Rating TC / Bureau) — universo = convertidos
- **Emissões por Grupo Especial — Share por Safra**: top 15 grupos
- **Resumo Analitico — Emissões**: performance, alertas, oportunidades, insights, acoes, chips

Filtros aplicados pela query base: `FLAG_CONVERSAO='1. Convertido'` + `DATE_DIFF(DT_CONV, DT_ENCENDIDO, DAY) <= 30`.

### Aba **Encendidos** (id `tab-encendidos`; montada via Claude Cowork)
- KPI panels lado a lado: Mes Atual vs Mes Anterior (4 cards cada: Encendidos / 1o Encendido / Reencendidos / % Adoption)
- **Evolucao por Safra**: barras stacked (1o vs Reenc) + tabela transposta (Encendidos / Convertidos / % Adoption em 8 meses)
- **Evolucao Diaria**: barras enc/dia + linhas canc-risco/dia e conv/dia (so para o mes corrente; placeholder em outros meses)
- **Share por Safra** (4 graficos 100%-stacked): NISE / App Ativo / Rating TC / Bureau
- **Encendidos por Grupo Especial — Share por Safra**: tabela transposta top 15 grupos com nominal + share %
- **Resumo Analitico** data-driven: Performance, Alertas (Cuentas Canceladas, queda adoption, taxa cancel risco), Oportunidades, Insights, Acoes, Chips

---

## 2. Arquitetura de arquivos

```
C:\Users\GPEREIRADOSS\
  CLAUDE.md                          ← regras globais p/ qualquer chat
  run_dashboard.py                   ← WRAPPER (so delega; nao edite)
  atualizar_dashboard.bat            ← bat da task scheduler
  dashboard_log.txt                  ← logs das runs automaticas
  .claude/projects/.../memory/
    MEMORY.md                        ← index de memorias persistentes
    project_dashboard.md             ← contexto deste projeto

  dashboard-emissoes/                ← REPO (GitHub: dashboard-emissoes)
    index.html                       ← dashboard publicado (Pages)
    dashboard_sg.sql                 ← SQL Emissoes (super_grupo)
    proj_historico.json              ← projecoes salvas por dia
    update_dashboard.py              ← UNICO entrypoint de update Emissoes
    STATE.md                         ← este documento
    .gitignore
    .nojekyll
```

---

## 3. Fluxo de update (executado pela task scheduler)

```
09:00 (e 09:30 backup)
  └── Task "AtualizarDashboard"
       └── atualizar_dashboard.bat
            ├── cd dashboard-emissoes
            ├── set CLOUDSDK_PYTHON env
            ├── py update_dashboard.py
            │    ├── BQ query (dashboard_sg.sql)
            │    ├── parse CSV → actual_data, proj_data
            │    ├── update proj_historico.json
            │    └── PATCH IN-PLACE index.html (preserva Encendidos):
            │         - MONTHS_ACTUAL (so do mes corrente)
            │         - MONTHS_PROJ
            │         - todayStr do MONTHS_META[mes corrente]
            │         - KPI values (kv-*-full, kv-*-micro, gt-*)
            │         - "Atualizado: DD/Mes/AAAA"
            │         - "Projecao hoje (DD/Mes)" e "ate DD/Mes"
            └── git add + commit + push (so se houver mudancas)
```

Tudo o que diz respeito a UI do dashboard ou aos dados Encendidos NAO e tocado pelo update automatico.

### Update Encendidos (manual via Claude)

Os dados ENC = {...} (monthly, nise, app, rating, bureau, grupos, funnel, daily, top_groups) sao snapshots, atualizados sob demanda via queries adicionais sobre `meli-bi-data.SBOX_CREDITSTC.base_projecao_emissao_igor`. Quando precisar refrescar, pedir ao Claude.

---

## 4. Convencoes criticas (NAO QUEBRAR)

### 4.1 SG_ORDER (super_grupos)
Tanto no Python quanto no JS, devem coincidir com o que o SQL devolve:
```
['BAU', 'EA', 'Sellers', 'Cuentas Canceladas', 'Only Nav', 'Mar Aberto']
```
- Nao usar 'CC' como abreviacao
- Linhas com super_grupo fora dessa lista caem no fallback (`else 'BAU'`) e contaminam o BAU

### 4.2 MONTHS_META.todayStr
Determina o ponto de corte entre barra (real) e linha (projecao) no grafico Emissoes.
- Deve ser sempre o dia corrente do mes vigente (`YYYY-MM-DD`)
- O `update_dashboard.py` ja patcha automaticamente, nao toque manualmente
- Se ficar desatualizado, dias passados aparecem como projecao (encendidos "somem" das barras)

### 4.3 Patch in-place vs regenerate
- **NUNCA** voltar a regenerar o `index.html` do zero (era o que o run_dashboard.py antigo fazia e destruia Encendidos)
- Toda atualizacao deve ser via `update_dashboard.py` (patches surgicos por regex)
- Se for adicionar um campo dinamico novo: dar id ao elemento HTML, criar funcao JS que popula, chamar no load + no setMonth

### 4.4 TDZ (Temporal Dead Zone) no JS
`let`/`const` nao podem ser referenciados antes da declaracao.
- Toda chamada de inicializacao (`renderFatores(ACTIVE_MONTH)`, etc.) **deve ser feita no final do script**, em try/catch
- Funcoes (`function foo(){}`) sao hoisted; `let`/`const` NAO

### 4.5 PLAN_BY_MONTH
Hardcoded no JS para cada mes:
```js
const PLAN_BY_MONTH = {
  '2026-04': {'TC Full': null, 'Micro TC': null},
  '2026-05': {'TC Full': 452000, 'Micro TC': 98000},
  '2026-06': {'TC Full': null, 'Micro TC': null}
};
```
Quando vier um plano novo, editar essa estrutura no `index.html` direto.

### 4.6 Aba Encendidos é "mes-aware"
Variavel global `ACTIVE_ENC_MONTH`. Cada render (`renderKPIs`, `renderEvolTable`, `renderGruposTable`, `renderAnalise`, `makeDailyChart`) chama `encIdx()` que retorna `{ml, pi, yi}` dependentes de `ACTIVE_ENC_MONTH`. Quando setMonth executa, propaga e chama todos os renders.

### 4.7 Aba Emissões é "mes-aware" (espelha 4.6)
Variavel global `ACTIVE_EMI_MONTH`. Renders: `renderEmiKPIs`, `makeEmiEvolChart`, `renderEmiEvolTable`, `makeEmiDailyChart`, `makeEmiShareChart`, `renderEmiGruposTable`, `renderEmiAnalise`. Helper `emiIdx()`. `setMonth` propaga para `ACTIVE_EMI_MONTH` quando o mes esta em `EMI_MONTHS`. Os 8 meses sao os mesmos de ENC. Dados em `const EMI = {...}` (snapshot, refresh sob demanda — mesma logica de ENC).

---

## 5. Bugs historicos (LICOES APRENDIDAS)

| # | Bug | Causa raiz | Fix |
|---|-----|-----------|-----|
| 1 | Aba Encendidos sumiu apos run_dashboard.py | Script antigo regenerava HTML do zero | Trocado por update_dashboard.py com patches in-place |
| 2 | Grafico nao renderizava | 1 chave `}` extra em makeShareChart/makeEvolChart | Brace check antes de commit |
| 3 | % invertido nos Share charts | `getPixelForValue(100-cum)` invertia stacking | Trocado para `getPixelForValue(cum)` / `getPixelForValue(cum+v)` |
| 4 | EA sumiu dos graficos | SG_ORDER Python = ['BAU','CC',...] (faltava EA, CC errado) | SG_ORDER = lista certa (ver §4.1) |
| 5 | Encendidos nao abria | `renderFatores(ACTIVE_MONTH)` chamado antes do `const PLAN_BY_MONTH` (TDZ ReferenceError) | Chamada movida pro fim do script em try/catch |
| 6 | Encendidos de ontem somiam do grafico | `todayStr` ficou estatico em ontem | Patch automatico no update_dashboard.py (ver §4.2) |
| 7 | Cuentas Canceladas alert era falso-positivo | Estava lendo do grupo BAU-CUENTAS CANCELADAS (segmentacao) em vez de status_cancelada_no_mes_de_encendido (cancelamento real pelo risco) | Refactor renderAnalise pra usar funnel.canc_mes |
| 8 | Fatores de Projecao repetia entre meses | Texto patcheado estatico no HTML | renderFatores dinamico via JS, calcula a partir de MONTHS_ACTUAL + PLAN_BY_MONTH |

---

## 6. Como mudar coisas comuns

### Adicionar mes novo (ex: Jun/2026)
1. Aguardar o primeiro dia do mes — o `update_dashboard.py` automaticamente adiciona o mes ao `MONTHS_ACTUAL` e atualiza `MONTHS_META`
2. Editar o `index.html` adicionando o botao no `month-selector`:
   ```html
   <button class="month-btn" data-month="2026-06" onclick="setMonth('2026-06')">Jun/2026</button>
   ```
3. Adicionar plano em `PLAN_BY_MONTH` (se houver)
4. Para a aba Encendidos suportar Jun: precisa re-rodar os refreshes ENC (queries adicionais sobre `base_projecao_emissao_igor`). Pedir ao Claude.

### Mudar regra de risco / categorias
Atualizar SQL (`dashboard_sg.sql`) e/ou queries de refresh ENC. Manter SG_ORDER consistente entre Python e JS.

### Adicionar nova metrica ao Resumo Analitico
Editar `renderAnalise` no `index.html`. Os dados ENC ja estao na pagina (monthly, funnel, daily, shares, grupos).

### Debug: aba Encendidos nao abre
1. Abrir DevTools → Console
2. Procurar ReferenceError (geralmente TDZ por reordenacao de declaracoes)
3. Brace check: rodar script de verificacao no script JS

---

## 7. Task scheduler

| Task | Hora | Acao | Estado |
|---|---|---|---|
| AtualizarDashboard | 09:00 + 09:30 | `atualizar_dashboard.bat` | Ready ✅ |

Apenas 1 task ativa, com dupla trigger (redundancia caso a primeira falhe).

---

## 8. TODO / Backlog conhecido

- [ ] Daily Pipeline em meses passados — hoje so funciona pro mes corrente
- [ ] Plano de Jun/2026 (quando definido pelo time)
- [ ] Botao Jun/2026 (criar quando mes virar)
- [ ] Considerar consolidar `_refresh_*.py` (3 scripts diferentes que rodei pra Encendidos) num so

---

## 9. Onboarding rapido (para novos chats)

1. Leia este STATE.md inteiro
2. Para qualquer alteracao no dashboard: editar `index.html` (UI) e/ou `update_dashboard.py` (dados)
3. SEMPRE rode brace check no JS antes de commitar (`<script>...</script>` deve ter depth=0)
4. Para refrescar dados de Encendidos via BQ, ver §6
5. Para entender o que NAO fazer, ver §5 (bugs historicos)
