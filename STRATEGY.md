# INTRA – Strategie-Dokumentation

## Strategien

### 1. Noise Breakout (NB)

**Grundlage:** Zarattini, Aziz & Barbon (2024)

Tagespreisbewegungen innerhalb einer "Noise Boundary" um den Daily Open sind statistisch Rauschen. Ein Ausbruch darüber hinaus signalisiert echtes Momentum.

**Noise Boundary:**
- Breite = Durchschnitt der absoluten Tagesrenditen (90 Tage) × Daily Open
- Fallback bei zu wenig Daten: ATR(15min) × 0.6
- Obere Grenze = Daily Open + Breite
- Untere Grenze = Daily Open − Breite

**Einstieg:**
- Prüfung nur bei Minute :00 und :30 (laut Paper)
- Close bricht über obere Grenze → LONG
- Close bricht unter untere Grenze → SHORT
- Mindest-Breakout-Stärke: 0.3 × ATR jenseits der Boundary

**Konfidenz-Score (0.0–1.0):**
- Breakout-Stärke (40%): wie weit jenseits der Boundary in ATR-Einheiten
- Volumen-Bestätigung (30%): Volume/MA-Ratio > 1.0
- MACD-Histogramm-Richtung (15%): bestätigt die Breakout-Richtung
- ADX über seinem Rolling Median (15%): bestätigt Trendstärke

**Ausstieg:** Trailing Stop, EOD Close, oder SL (kein fester Take Profit)

**Timing:**
- Max 1 NB-Trade pro Asset pro Tag
- NB stoppt 60 Minuten vor Session-Close (Gao übernimmt)

---

### 2. Gao Momentum

**Grundlage:** Gao, Han, Li & Zhou (2018) – Intraday Momentum Effekt

Wenn der Kurs sich tagsüber signifikant vom Vortags-Close entfernt hat, setzt sich diese Bewegung in den letzten 30 Minuten der Session fort.

**Einstieg:**
- Einmal pro Asset pro Tag, 30 Minuten vor Session-Close (One-Shot)
- Bedingung: |Tagesrendite| ≥ 0.1% (Previous Close → aktueller Preis)
- Positive Rendite → LONG, negative Rendite → SHORT
- Konfidenz: 0.5 bei 0.1% Schwelle, steigt linear bis 1.0 bei 1.0% Bewegung

**Ausstieg:** Trailing Stop, EOD Close (5 min vor Session-Ende), oder SL

**Gao Handover:**
Wenn eine NB-Position offen ist, wird sie 30 Minuten vor Close automatisch geschlossen, um Platz für Gao zu machen.

---

## Entscheidungs-Pipeline

```
1min Candle kommt rein
  │
  ├─ Indikatoren aktualisieren
  ├─ Korrelations-Tracker füttern
  ├─ Tageswechsel prüfen
  │
  ▼
PRE-TRADE CHECKS (10 Prüfungen, siehe Filter)
  │── FAIL → STOP
  │
  ▼
Circuit Breaker (3× SL hintereinander → Pause)
  │── FAIL → STOP (nur NB, Gao ist unabhängig)
  │
  ▼
SIGNAL-ERKENNUNG
  ├─ Minute :00 oder :30 UND >60min bis Close UND Regime ≠ BLOCKED?
  │   └─ JA → Noise Breakout Check
  │
  ├─ Kein NB-Signal UND 30–5 min vor Close UND noch nicht gecheckt heute?
  │   └─ JA → Gao Momentum Check (One-Shot)
  │
  ├─ Kein Signal → STOP
  │
  ▼
NACHPRÜFUNGEN
  ├─ Tages-Trade-Limit (1 NB + 1 Gao pro Asset)
  ├─ Signal-Cooldown (10 min zwischen gleichen Signalen)
  ├─ Globaler Cooldown (60 sek zwischen allen Trades)
  ├─ Korrelations-Limit (eff. Positionen < 2.5)
  │── FAIL → STOP
  │
  ▼
TRADE VALIDIERUNG
  ├─ SL berechnen (ATR-basiert, Kurtosis/Volatilitäts-angepasst)
  ├─ Spread-Filter (SL ≥ 3× Spread)
  ├─ EV-Gate (nach 20 Trades: EV > 0 erforderlich)
  │── FAIL → STOP
  │
  ▼
POSITION SIZING (Half-Kelly / Basisrisiko, korrelationsangepasst)
  │── Zu klein → STOP
  │
  ▼
TRADE AUSFÜHREN (Capital.com API)
```

---

## Filter & Constraints

### Generelle Filter (blocken beide Strategien)

| Filter | Regel |
|--------|-------|
| Kill Switch | Manuelle Notbremse |
| Bot gestoppt | Bot ist nicht im Running-State |
| Wochenende | Samstag/Sonntag, keine Märkte |
| Daily Loss Limit | Drawdown ≥ 3% vom Tagesstart-Balance |
| Position offen | Ein Trade pro Instrument gleichzeitig |
| Max Positionen | Maximal 4 gleichzeitig |
| Margin zu niedrig | Verfügbar < 20% des Eigenkapitals |
| Spread zu hoch | Spread > 1.5× Durchschnittsspread |
| Session geschlossen | Außerhalb der Handelszeiten |
| Session noch nicht offen | Vor der Session-Öffnung |
| News Blackout | 15 min vor High-Impact Wirtschafts-Events |

### Strategie-spezifische Filter

| Filter | NB | Gao | Begründung |
|--------|-----|-----|------------|
| Session Close Buffer (30 min) | geblockt | durchgelassen | Gao handelt genau in diesem Fenster |
| BLOCKED Regime | geblockt | durchgelassen | Hohe Volatilität ist Gao's Vorteil, kurze Haltezeit |
| Circuit Breaker (3× SL) | geblockt | durchgelassen | NB-Verluste sind irrelevant für unabhängige Gao-Strategie |

---

## Regime-System

Basiert auf 15-Minuten-Daten. Wird in dieser Reihenfolge geprüft:

**BLOCKED** – Extrembedingungen, unsicher für NB:
- Return-Kurtosis > 5.5 (Fat Tails), ODER
- ADX ≥ 35 UND schnell steigend UND (Volumen > 2× Durchschnitt ODER Bollinger Bänder expandieren > 1.3×)

**BULLISH** – EMA9 > EMA21, ADX über Median, EMA-Spread ≥ 0.15 × ATR

**BEARISH** – EMA9 < EMA21, ADX über Median, EMA-Spread ≥ 0.15 × ATR

**NEUTRAL** – Alles andere (niedriger ADX, konvergierende EMAs)

> Bias ist informativ für NB – nur BLOCKED verhindert tatsächlich NB-Trades (paper-konform, rein mechanisch).

---

## Indikatoren

Zwei Indikator-Suiten pro Instrument (1min und 15min), inkrementell berechnet:

| Kategorie | Indikatoren |
|-----------|-------------|
| Trend | ADX(14) mit +DI/-DI, EMA(9), EMA(21), EMA(50) |
| Volatilität | Bollinger Bands(20, 2.0), ATR(14), BB-Breite + MA |
| Momentum | RSI(14), MACD(12,26,9), Stochastic(14,3) |
| Volumen | OBV, Volume Delta, Volume MA Ratio(20), VWAP (täglicher Reset) |
| Mikrostruktur | Body/Range Ratio, 5-Candle Return, Return-Kurtosis, OBV-Slope, Spread/ATR Ratio |
| Divergenz (nur 15min) | RSI- und MACD-Divergenzerkennung |
| Candlestick (nur 1min) | Hammer, Shooting Star, Doji, Engulfing, Strong Bull/Bear |
| Adaptiv | RSI-Perzentil (Rolling 100), ADX-Median (Rolling 20) |

**Noise Boundary:** Daily Open ± Durchschnitt(|Tagesrendite|, 90 Tage) × Daily Open

---

## Risikomanagement

### Position Sizing
- **Basisrisiko:** 1.5% des Kontostands pro Trade
- **Kelly-Ramp:** Ab 50 Trades wird Half-Kelly eingeblendet, ab 100 Trades voll aktiv (max 3%)
- **Negativer Edge:** EV < 0 nach 20+ Trades → Risiko sinkt auf 0.5%
- **Korrelationsanpassung:** Bei eff. korrelierten Positionen ≥ 2 wird Risiko skaliert (Faktor 2/Anzahl)
- **Leverage-Cap:** Max Notional = 3× Balance

### Stop Loss
- SL = 1.5 × ATR(15min)
- Kurtosis-Anpassung: bei Kurtosis > 3 wird SL verbreitert
- Volatilitäts-Anpassung: skaliert mit ATR/ATR-Durchschnitt (0.7–1.5×)
- Spread-Filter: SL muss ≥ 3× Spread sein

### Take Profit
- Kein echter TP – nur ein Safety-Net bei 10× ATR als Broker-Backstop

### Trailing Stop
- Aktivierung bei +0.75R (R = Risikobetrag)
- Trail-Distanz = max(1.2 × ATR(1min), statistisch optimierte Distanz)
- Optimale Distanz = 30. Perzentil der Gewinntrades / Ø Verlust (adaptiert sich)

### EOD Close
- NB-Positionen: 30 min vor Session-Close geschlossen (Gao Handover)
- Alle Positionen: 5 min vor Session-Close zwangsgeschlossen

### Circuit Breaker
- 3 konsekutive SL-Hits auf einem Instrument → Pause für Rest der Session
- Gilt nur für NB, Gao ist davon unabhängig

### Scratch Trades
- Trades mit |PnL| < 0.3 × SL-Distanz gelten als "Rauschen"
- Zählen für P&L, aber nicht für Win/Loss-Statistik oder Circuit Breaker

---

## Instrumente & Sessions

| Instrument | Session (UTC) | Gao-Fenster (UTC) | EOD Close (UTC) |
|------------|---------------|-------------------|-----------------|
| DE40 | 00:00 – 21:00 | 20:30 – 20:55 | 20:55 |
| FR40 | 00:00 – 21:00 | 20:30 – 20:55 | 20:55 |
| UK100 | 00:00 – 21:00 | 20:30 – 20:55 | 20:55 |
| US100 | 00:00 – 21:15 | 20:45 – 21:10 | 21:10 |
| US500 | 00:00 – 21:15 | 20:45 – 21:10 | 21:10 |

**Buffers:**
- Opening Buffer: 30 min (keine neuen Trades in den ersten 30 min)
- NB Cutoff: 60 min vor Close (keine neuen NB-Trades)
- Session Close Buffer: 30 min vor Close (nur NB geblockt, Gao erlaubt)
- Force Close: 5 min vor Session-Ende

---

## EV-Gate & Statistik

- **Bootstrap-Phase:** Erste 20 Trades pro Strategie-Typ → EV-Gate deaktiviert, Basisrisiko
- **Post-Bootstrap:** Spread-adjustierter EV muss > 0 sein, sonst wird nicht gehandelt
- **Per-Epic Gating:** Ab 5 Trades pro Instrument wird auch der epic-spezifische EV geprüft
- **NB und Gao haben getrennte Statistiken** – jede Strategie hat eigene Win-Rate, EV, und Kelly-Sizing
