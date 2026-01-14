# BTC Trading Mind: Unified Architecture Specification

## 1. System Architecture

The system operates as a synchronous loop where "Thinking" is triggered by "Events". This differs from linear bots by implementing a "Sense-Think-Act" cycle with a centralized World Model.

### Core Loop
1.  **Senses (Ingest):** Connects to Standard Binance API (Price, OrderBook, Trades) and News APIs. Updates `WorldModel`.
2.  **Attention (Filter):** Runs lightweight checks defined in `Attention Triggers`. Manages "Silence" (boredom detection).
3.  **World Model (Belief):** The single source of truth. Maintains probabilistic regime belief and psyche state.
4.  **Reasoner (Decide):**
    *   **Fast Path:** Heuristics (e.g., "Funding Shock" -> "Reduce Risk").
    *   **Slow Path:** Deep hypothesis generation (LLM/Tool-assisted).
5.  **Action (Execute):** Simulates execution (Paper Trading). Logs to SQLite. Handles simulated slippage and fees (Microstructure Safety).
6.  **Learning (Reflect):** Post-trade analysis using Process vs Outcome scores and Counterfactuals.

---

## 2. World Model Schema

The central shared state object representing the agent's complete belief system.

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "BTC Trading Mind World Model",
  "type": "object",
  "properties": {
    "meta": {
      "type": "object",
      "properties": {
        "timestamp": { "type": "string", "format": "date-time" },
        "tick_id": { "type": "integer" },
        "latency_ms": { "type": "integer", "description": "Time from exchange timestamp to ingest" }
      }
    },
    "features": {
      "description": "Numeric derived metrics used for triggers and learning. Normalized where possible.",
      "type": "object",
      "properties": {
        "volatility": {
          "type": "object",
          "properties": {
            "atr_1m": { "type": "number" },
            "atr_1h": { "type": "number" },
            "vol_z_score": { "type": "number", "description": "Current vol deviation from rolling 24h mean" }
          }
        },
        "order_book": {
          "type": "object",
          "properties": {
            "bid_ask_spread_pct": { "type": "number" },
            "depth_imbalance_1pct": { "type": "number", "description": "(BidVol - AskVol) / TotalVol within 1% of mid" },
            "book_pressure_z": { "type": "number" }
          }
        },
        "liquidity": {
          "type": "object",
          "properties": {
            "sweep_score": { "type": "number", "description": "Strength of recent liquidity sweep event" },
            "nearest_liquidity_void_dist": { "type": "number", "description": "Distance to nearest 50% drop in depth" }
          }
        }
      }
    },
    "evidence": {
      "description": "Provenance for all beliefs. Pointers to raw data sources.",
      "type": "object",
      "properties": {
        "primary_price_feed": { "type": "string", "description": "e.g., binance:BTC/USDT" },
        "snapshot_id": { "type": "string", "description": "Hash or ID of the orderbook snapshot used" },
        "active_headline_ids": { "type": "array", "items": { "type": "string" } },
        "last_trade_id": { "type": "string" }
      }
    },
    "market_perception": {
      "type": "object",
      "properties": {
        "confidence_score": { "type": "number", "minimum": 0, "maximum": 1, "description": "Data integrity score. Low if stale or thin liquidity." },
        "price": {
          "type": "object",
          "properties": {
            "mid": { "type": "number" },
            "mark": { "type": "number" },
            "last": { "type": "number" }
          }
        }
      }
    },
    "internal_state": {
      "type": "object",
      "properties": {
        "regime_belief": {
          "description": "Probabilistic distribution of current market regime.",
          "type": "object",
          "properties": {
            "TRENDING_UP": { "type": "number", "minimum": 0, "maximum": 1 },
            "TRENDING_DOWN": { "type": "number", "minimum": 0, "maximum": 1 },
            "RANGE_BOUND": { "type": "number", "minimum": 0, "maximum": 1 },
            "CHOP": { "type": "number", "minimum": 0, "maximum": 1 }
          },
          "required": ["TRENDING_UP", "TRENDING_DOWN", "RANGE_BOUND", "CHOP"]
        },
        "psyche": {
          "type": "object",
          "properties": {
            "state": { "type": "string", "enum": ["NEUTRAL", "DEFENSIVE", "AGGRESSIVE", "COOLDOWN", "FROZEN"] },
            "risk_aversion_multiplier": { "type": "number", "default": 1.0 },
            "boredom_counter": { "type": "number", "description": "Increases with time spent in 'No Trade' loop" },
            "frustration_level": { "type": "number", "minimum": 0, "maximum": 1 }
          }
        },
        "risk_state": {
          "type": "object",
          "properties": {
            "global_drawdown_pct": { "type": "number" },
            "exposure_pct": { "type": "number" },
            "daily_loss_limit_remaining": { "type": "number" },
            "trading_allowed": { "type": "boolean" }
          }
        }
      }
    },
    "last_decision": {
      "description": "Snapshot of the last cognitive decision cycle, regardless of action.",
      "type": "object",
      "properties": {
        "id": { "type": "string", "format": "uuid" },
        "timestamp": { "type": "string" },
        "trigger_event": { "type": "string" },
        "intention": { "type": "string", "enum": ["OBSERVE", "HUNT_LONG", "MANAGE_POSITION", "ESCAPE", "SLEEP"] },
        "hypothesis": { "type": "string" },
        "expected_outcome": {
           "type": "object",
           "properties": {
             "win_probability": { "type": "number" },
             "reward_risk_ratio": { "type": "number" }
           }
        },
        "invalidation_condition": { "type": "string" }
      }
    }
  },
  "required": ["meta", "features", "evidence", "internal_state"]
}
```

---

## 3. Attention Triggers

Configuration for noise filtering and event escalation.

```yaml
global_settings:
  silence_timeout_sec: 300  # If no trigger for 5m, force "Deep Scan"
  default_debounce_ms: 1000

triggers:
  - name: "Vol_Explosion"
    group: "MARKET_STRUCTURE"
    priority: "HIGH"
    condition: "features.volatility.atr_1m > (3.0 * features.volatility.atr_1h)"
    debounce_ms: 5000
    hysteresis: 
      feature: "features.volatility.atr_1m"
      reset_threshold_mult: 2.0 # Must account for time decay (Vol Clustering) to prevent flickering
    response: "WAKE_REASONER_REGIME_CHECK"
    desc: "Sudden expansion in volatility suggesting regime shift or breakout."

  - name: "Liquidity_Sweep"
    group: "ORDER_FLOW"
    priority: "CRITICAL"
    condition: "features.liquidity.sweep_score > 0.8 && features.internal_state.regime_belief.RANGE_BOUND > 0.6"
    debounce_ms: 10000
    response: "TRIGGER_HYPOTHESIS_TRAP"
    desc: "Price broke localized level but volume/delta reversed immediately."

  - name: "Flash_News_Tier1"
    group: "NARRATIVE"
    priority: "MAX"
    condition: "event.news.tier == 'TIER_1' && event.news.sentiment_abs > 0.5"
    debounce_ms: 60000
    response: "HALT_ENTRIES_AND_ANALYZE"
    desc: "Major news event (SEC, FED, HACK). Immediate risk protocol."

  - name: "Order_Book_Void"
    group: "LIQUIDITY_RISK"
    priority: "HIGH"
    condition: "features.order_book.nearest_liquidity_void_dist < 0.2"
    response: "SWITCH_PSYCHE_DEFENSIVE"
    desc: "Liquidity evaporated near price. High slippage risk. Widen tolerance or pause."

  - name: "Whale_Agression"
    group: "ORDER_FLOW"
    priority: "MEDIUM"
    condition: "trade.size_usd > 5000000 && trade.aggressor_side == features.order_flow.dominant_side"
    debounce_ms: 1000
    response: "LOG_AND_WATCH_CONTINUATION"
    desc: "Large aggressor trade aligned with order flow bias."

  - name: "Silence_Deep_Scan"
    group: "SYSTEM"
    priority: "LOW"
    condition: "system.last_trigger_time > global_settings.silence_timeout_sec"
    response: "PERFORM_FULL_STATE_RECALIBRATION"
    desc: "Nothing happened for N minutes. Wake up to check if baselines shifted or feed died."
```

---

## 4. Evaluation & Psyche Protocols

Rules for scoring decision quality, managing agent psychology, and learning.

### Decision Quality Score ("Process Score")
**Target:** Consistently > 90/100. Measures adherence to logic, not PnL.

| Metric | Max Points | Logic |
| :--- | :--- | :--- |
| **Plan Adherence** | 40 | Entered/Exited exactly as `DecisionRecord` planned (-10 pts per deviation). |
| **Regime Congruence** | 20 | Strategy valid for current `regime_belief`? |
| **Risk Integrity** | 20 | Position size <= `max_risk`. |
| **Evidence Strength** | 20 | `confidence_score` > required threshold. |

### Outcome Classifications
*   **Perfect Trade:** Process > 90, PnL > 0.
*   **Good Loss:** Process > 90, PnL < 0. (Variance).
*   **Sloppy Win:** Process < 70, PnL > 0. (Do not reinforce).
*   **Toxic Trade:** Process < 50. (Engage Cooldown).

### Psyche Modules

**1. The "Boredom Throttle"**
*   **Trigger:** `Boredom_Counter` > Threshold (no trades for X mins).
*   **Action:** Decrease `Risk_Multiplier`, Increase `Trigger_Thresholds`.
*   **Reset:** On `Vol_Explosion`.

**2. The "Tilt Breaker" (Cooldown)**
*   **Trigger:** 2 consecutive "Toxic Trades" OR `Drawdown > Daily_Limit`.
*   **Action:** Set `Internal_State.Psyche.State` = `COOLDOWN`. Read-only mode for N hours. Requires 3 correct hypothetical predictions to unlock.

### Counterfactual Memory
For every executed trade, simulate and store:
1.  **Null Hypothesis:** PnL if we did nothing.
2.  **Inverse Hypothesis:** PnL if we took the opposite trade.
3.  **Passive Execution:** PnL if strict limit orders were used.

**Learning Rule:** If `Inverse > Actual` consistently for a Trigger+Regime, invert signal weight.

---

## 5. Implementation Roadmap

### Phase 1: The Observer (Data & State)
*   **Project Setup:** Initialize Python 3.11+ environment, git repo, and directory structure.
*   **State Management:** Implement `WorldModel` using **Pydantic** for rigorous schema validation.
*   **Data Ingestion:**
    *   **Research:** Check online which tools/APIs are available (e.g., Binance Order Book API specifics) before implementation.
    *   Setup `ccxt` (Sync) for Binance Spot REST API polling.
    *   Build `FeatureEngine` to calculate rolling Z-scores and ATRs using `pandas`/`numpy`.
    *   Persist raw tick data to **SQLite** (using `SQLAlchemy` sync).
*   **Attention System:** Implement the `Attention` logic.
*   **Visualization:** Create a TUI (Terminal User Interface) using **Textual** to visualize "Mind State" real-time.
*   **Deliverable:** Functioning "Observer" with live TUI dashboard and SQLite data recording.

### Phase 2: The Analyst (Reasoning & Evidence)
*   **Decision Logging:** Create `Decisions` table in SQLite to store `DecisionRecord` objects.
*   **Evidence Chain:** Implement logic to snapshot OrderBook/News state and link to Decision IDs.
*   **Regime Detection:** Build `RegimeClassifier` (Simple heuristic-based initially) to update `regime_belief`.
*   **Deliverable:** System that logs "Hypothetical Decisions" with full evidence trails to the DB.

### Phase 3: The Trader (Execution & Psyche)
*   **Paper Exchange:** Implement a `MockExchange` class that simulates Binance execution (orders, fills, fees) against live data.
*   **Psyche Engine:** Implement the `Boredom` and `Tilt` state machines.
*   **Action Layer:** Connect "Analyst" decisions to "Paper Exchange" execution.
*   **Deliverable:** End-to-end Paper Trading Simulation running safely in a loop.

### Phase 4: The Learner (Optimization)
*   **Counterfactuals:** Implement a background job that analyzes past decisions against future price movements.
*   **Reporting:** Generate nightly performance reports (Markdown/HTML) from SQLite data.

---

## 6. Technical Stack Reference

*   **Language:** Python 3.11+
*   **Core:** Synchronous Execution Loop
*   **Data Models:** `pydantic` (Schema/Validation)
*   **Exchange:** `ccxt` (Synchronous REST)
*   **Database:** `sqlite` + `sqlalchemy` (Persistence)
*   **Analysis:** `pandas`, `numpy`, `ta-lib` (Features)
*   **UI:** `textual` or `rich` (Live Dashboard)
*   **Testing:** `pytest`
