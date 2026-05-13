# @bybit-aplus-hunter

Skill de Hermes para detectar entradas A+ en Bybit con orderflow cross-exchange confirmado.
Combina los 4 filtros técnicos del sistema + orderflow_signal_scorer.py para producir
un veredicto binario: ENTRAR o ESPERAR, con parámetros exactos de ejecución.

---

## Cuándo usar este skill

Invocarlo cuando:
- El agente detecta que precio se acerca a una zona operativa (Donchian High/Low ±0.5%)
- El usuario pide `/punk-hunt` en el profile bybit
- Hay una señal externa que necesita validación cross-exchange

---

## Fase activa y reglas de tamaño

Antes de ejecutar el pipeline, determinar la fase activa desde el capital actual:

| Capital en cuenta | Fase | Filtros requeridos | Score mínimo | Size |
|---|---|---|---|---|
| $50–$150 | 1 — Ataque | 3/4 (si orderflow ≥80, relaxar a 3) | 70 | $50 fijo |
| $150–$500 | 2 — Agresivo moderado | 4/4 obligatorios | 70 | 15–20% del capital |
| $500–$2,000 | 3 — Crecimiento | 4/4 + volumen OKX confirmado | 70 | 10–15% del capital |
| $2,000–$10,000 | 4 — Escala | 4/4 + confluencia multi-TF | 65 | 8–10% del capital |
| $10,000+ | 5 — Conservador agresivo | 4/4 + multi-TF + ratio 2:1 mínimo | 60 | 5% del capital |

---

## Pipeline de ejecución (seguir en orden)

### PASO 1 — Detectar régimen de mercado

```
/regime
```

Si régimen es VOLATILE → STOP. No operar. Fin del pipeline.
Si régimen es RANGE → usar Mean Reversion (Donchian + RSI + BB).
Si régimen es TRENDING → usar Donchian Breakout si breakout confirmado.

### PASO 2 — Verificar ADX

Leer ADX(14) en TF activo (15m por defecto):
- ADX < 20 → mercado sin dirección → ESPERAR
- ADX 20–25 → régimen débil → solo entrar si orderflow score ≥ 80
- ADX ≥ 25 → régimen claro → continuar pipeline

### PASO 3 — Validar 4 filtros técnicos

Para LONG (todos deben cumplirse):
1. Precio toca o cruza Donchian Low (dentro 0.1%)
2. RSI(14) < 35
3. Low de vela toca Bollinger Band inferior
4. Vela cierra verde (close > open)

Para SHORT (mirror):
1. Precio toca o cruza Donchian High (dentro 0.1%)
2. RSI(14) > 65
3. High de vela toca Bollinger Band superior
4. Vela cierra roja (close < open)

Anotar: cuántos de 4 filtros se cumplen.

**Fase 1 especial:** si orderflow score ≥ 80 y 3/4 filtros → puede continuar.
**Fase 2+:** si menos de 4/4 → STOP. No entrar.

### PASO 4 — Correr orderflow_signal_scorer.py

```bash
python3 scripts/orderflow_signal_scorer.py --side [long|short] --summary
```

Leer el score total y el breakdown:
- Score ≥ 70 → orderflow confirma → continuar
- Score 50–69 → señal débil → solo si Fase 1 y ADX ≥ 28, continuar con half size
- Score < 50 → orderflow contradice → STOP aunque técnico diga GO

**Regla dura:** nunca entrar contra el orderflow score si score < 50.

### PASO 5 — Calcular parámetros de entrada

Con setup aprobado, calcular:

```
Capital activo: [leer del profile]
Size en USD:    [según tabla de fase]
Leverage:       25x
Exposure:       Size × 25

SL distance:    ATR(14) × 1.5  [leer de TradingView]
SL precio:      entry ± SL_distance
TP1 precio:     entry ± (SL_distance × 2.5)   [cerrar 40%]
TP2 precio:     entry ± (SL_distance × 4.0)   [cerrar 40%]
TP3 precio:     entry ± (SL_distance × 6.0)   [cerrar 20%]

Riesgo USD:     Size × (SL_distance / entry_price) × 25
Ganancia TP1:   Size × ((TP1 - entry) / entry) × 25
```

### PASO 6 — Verificar stop del día

Revisar PnL del día actual:
- Fase 1: si PnL del día ≤ -$5 (−10% de $50) → BLOCK. No entrar.
- Fase 2: si PnL del día ≤ -$32 (−8% de $400) → BLOCK. No entrar.
- Fase 3+: si PnL del día ≤ −6% del capital → BLOCK. No entrar.

Si trades del día ≥ 3 → BLOCK. No entrar. Máx 3 trades por día en Fase 1–2.

### PASO 7 — Emitir veredicto

**Si todo aprobado:**

```
═══════════════════════════════════════════════
  ENTRADA A+ APROBADA ✓
═══════════════════════════════════════════════
  Par:        BTCUSDT.P (Bybit)
  Lado:       [LONG / SHORT]
  Fase:       [1–5]
  
  ORDERFLOW SCORE:  [X]/100 ✓
  FILTROS TÉCNICOS: [X]/4 ✓
  ADX:              [X] ✓
  RÉGIMEN:          [RANGE/TRENDING] ✓
  
  — Parámetros de ejecución —
  Entry:   $[precio]
  Size:    $[USD] (25x → $[exposure] exposure)
  SL:      $[precio]  (riesgo: $[USD])
  TP1:     $[precio]  (40% de la posición)
  TP2:     $[precio]  (40% de la posición)
  TP3:     $[precio]  (20% runner)
  
  DUREX: mover SL a BE cuando precio alcance 20% del recorrido a TP1
═══════════════════════════════════════════════
```

**Si rechazado:**

```
═══════════════════════════════════════════════
  SETUP RECHAZADO ✗
═══════════════════════════════════════════════
  Razón:   [motivo exacto del rechazo]
  Acción:  ESPERAR siguiente setup
  Re-check en: 30 minutos o cuando precio
               se aleje de la zona actual
═══════════════════════════════════════════════
```

---

## Señales de alerta que anulan la entrada

Aunque el score y filtros pasen, abortar si:
- Evento macro en las próximas 2h (FOMC, CPI, datos de empleo)
- Spread bid/ask > 0.05% en Bybit
- Open Interest cayó >5% en los últimos 15 min (desapalancamiento masivo)
- Funding rate > +0.05% (longs muy pesados, riesgo de squeeze)
- Dos SLs consecutivos en el día actual

---

## Logging obligatorio post-entrada

Cuando se ejecuta una entrada A+, registrar en `.claude/profiles/bybit/memory/signals_received.md`:

```markdown
## [FECHA HORA CR] — BTCUSDT.P [LONG/SHORT] 25x

**Fase:** [1–5]
**Score orderflow:** [X]/100
**Filtros técnicos:** [X]/4
**ADX:** [X]
**Régimen:** [RANGE/TRENDING]

**Parámetros:**
- Entry: $[precio]
- SL: $[precio] | TP1: $[precio] | TP2: $[precio] | TP3: $[precio]
- Size: $[USD] | Exposure: $[USD]

**Resultado:** [pendiente / TP1 / TP2 / TP3 / SL / manual]
**PnL:** $[USD]
**Aprendizaje:** [una línea]
```

---

## Comandos de referencia rápida

```bash
# Correr scorer completo
python3 scripts/orderflow_signal_scorer.py --side long --summary
python3 scripts/orderflow_signal_scorer.py --side short --summary

# Solo JSON para parseo
python3 scripts/orderflow_signal_scorer.py --side long --json

# Derivados de Binance (contexto adicional)
python3 scripts/derivatives_fetcher.py --summary

# Snapshot cross-exchange completo
python3 scripts/orderflow_snapshot.py
```

---

## Notas de implementación

- El scorer hace 7 llamadas HTTP a APIs públicas (sin API key). Timeout 8s cada una.
- Si más de 3 fuentes fallan (error de red), el veredicto default es ESPERAR.
- El score se pondera automáticamente si hay errores de fuentes (divide sobre disponibles).
- Compatible con profile bybit y cualquier par USDT.P listado en Bybit.
- Para pares distintos a BTCUSDT, editar las constantes SYMBOL_* en orderflow_signal_scorer.py.
