# Package C2 — Maker orders (post, wait, manage) instead of crossing the spread

**Status: STAGED — not implemented.** Do not start until the operator gives the
signal ("execute C2" or similar). Analysis done 2026-09-27 against commit
`e9f4a83` on the droplet (`/opt/kalshi/trading/kalshi-bot`).

Roadmap position: phase C, step 2. Step 1 (the WebSocket feed) went live on
2026-09-27 and is the prerequisite: a resting order can only be managed if
the bot sees the book move in real time.

---

## 1. Why

Every trade the bot has made so far crossed the spread twice: bought at the
ask, and (unless held to settlement) sold at the bid. On 26 September 8 of 9
consecutive losses were round trips that lost exactly the spread; of 30 closed
positions to date, 10 made money. The taker fee is also charged on both legs:

    taker fee = ceil_to_cent(0.07 * C * P * (1 - P))     e.g. 25 @ 50c = $0.44
    maker fee = ceil_to_cent(0.0025 * C * P)  (config)   e.g. 25 @ 50c = $0.04

A maker order - a limit price that RESTS on the book and waits to be taken -
captures part of the spread instead of paying it, and pays the (much smaller,
sometimes zero) maker fee. On a 5-10c demo spread that is the difference
between a forecast edge surviving execution and not.

**Honest limits, stated up front:**

- **It cannot create an edge.** It only stops execution from eating one. The
  weather strategy is currently not permitted to enter anywhere (phase A:
  it has not yet shown it beats the market). Maker orders make a proven edge
  cheaper to take; they do nothing for an unproven one.
- **Adverse selection.** A resting order fills when someone else wants the
  other side - disproportionately when the market is about to move against
  it. Fill rate and post-fill drift have to be MEASURED, not assumed.
- **The demo exchange cannot answer the fill question.** Checked 2026-09-27:
  the production trade tape for `KXHIGHNY-26SEP27-B69.5` showed real prints;
  the demo tape for the same market was empty. A resting demo order may
  simply never fill. Hence phase M0 measures on production data first.

---

## 2. Audit — how orders work today

| Piece | File | Today | Maker-relevant gap |
|---|---|---|---|
| Order submit | `clients.DemoExecutionClient.place_demo_order` → `client.create_order` | V2 `POST /portfolio/events/orders`, limit, `time_in_force` good_till_canceled, `post_only` **supported and sent when True** | Nobody passes `post_only=True` |
| Entry execution | `execution_manager.ExecutionManager.execute` | Submit all legs at the ask, poll fills for `max_partial_fill_time_seconds` (8 s), cancel the remainder | Built for immediate fills; a resting order is cancelled after 8 s |
| Exits | `position_manager.PositionWorker._exit` | Sell at the bid, reduce-only clamp | Every exit crosses the spread, urgent or not |
| Fee model | `fees.FeeModel` | `assume_taker: true` - every trade priced as taker, x1.10 safety | Maker fee is modelled but never used |
| Fill detection | `get_order` polling | Poll every 0.6 s during the 8 s window | No tracking of orders that outlive a scan |
| Market view | `market_state` + `wsfeed` (live since 2026-09-27) | Books current to the last delta | **Ready** - this is what makes management possible |
| Trade tape | Kalshi `GET /markets/trades` (public, production) | Not read | Has `taker_side` per print: exactly what a fill simulation needs |

Structural arbitrage stays **taker-only**: a multi-leg basket that rests is a
basket that can half-fill and sit unhedged. Nothing in this package changes it.

---

## 3. Design

### 3.1 What rests, what crosses

| Order | Maker? | Why |
|---|---|---|
| Forecast entry (single leg) | **Yes** | No urgency; the edge is a view on the day, not the next second |
| Exit: take-profit, edge decayed, reduce | **Yes, with a deadline**, then taker | Not urgent; spread capture is pure gain |
| Exit: stop-loss, closing soon, thesis gone | **No - taker** | Urgent by definition; a stop that waits is not a stop |
| Arbitrage legs | **No - taker** | Leg risk (above) |

### 3.2 Pricing a resting entry

Buy side example (buying YES; NO mirrors on the YES book as today):

    fair      = our probability (cents)
    best_bid  = current best YES bid
    best_ask  = current best YES ask
    price     = min(best_bid + improve_cents,            # join/improve the bid
                    fair - min_edge_cents - maker_fee,   # never pay away the edge
                    best_ask - 1)                        # never cross (post_only
                                                         # would reject anyway)

If `price` is below `best_bid` the order would sit behind the queue - allowed
(it may still fill) but recorded. If there is no positive-edge price at all,
no order.

### 3.3 Managing a resting order (new: `kalshibot/order_manager.py`)

One worker, on the feed's clock, owning every resting order:

- **Re-price** when the best bid/ask moves by >= `reprice_threshold_cents`,
  keeping the edge constraint; at most `max_reprices` times.
- **Cancel** when: our estimate moves and the edge is gone; the market-skill
  or data gates would now reject the entry; `max_rest_seconds` passes; the
  market is within `close_within_minutes` of closing; the feed is down (a
  resting order we cannot watch is a resting order we should not have).
- **Partial fills** are fine for a single leg: what filled is a (smaller)
  position, handed to the position manager as today (`PARTIAL_FILLED`).
- **Fill detection**: poll `get_order` for each resting order every few
  seconds (simple, works today). The authenticated WS `fill` channel is a
  later optimisation, not a dependency.
- **Every state change is journalled** (new table `resting_orders`:
  placed/repriced/filled/cancelled, price, queue position estimate, reason) -
  so fill rate, time-to-fill and post-fill drift are measurable.
- **On restart**: cancel every resting order the bot owns (client_order_id
  prefix) before trading, so a crash never leaves orphans working.

### 3.4 Risk limits

Resting orders count against exposure **at their full size** from the moment
they are placed (they can fill at any time), using the same per-market and
total exposure limits and the same reduce-only clamp as today. The circuit
breaker and calibration/market-skill suspensions cancel all resting entries.

---

## 4. Phases (each its own commit; measure before enforcing)

### M0 — Shadow maker simulation on production data (no orders)
- New worker reads the **production public trade tape** (read-only, no
  credential, through the same allowlisted reader as the execution probe) for
  the markets the forecast strategy prices.
- For every entry the strategy WOULD make (traded or blocked by the gates),
  simulate the maker order of 3.2: would it have filled, when, how much, and
  where did the price go 5/30/60 minutes after the fill (adverse selection)?
- Journal it (`maker_shadow`), report it (`scripts/maker_report.py`).
- **Exit criterion:** 1-2 weeks of data; decision on whether maker entries are
  worth building, and the parameters (improve_cents, max_rest_seconds).

### M1 — Order manager infrastructure (demo, behind a flag, off)
- `order_manager.py`, `resting_orders` table, startup cancel-all of own
  orders, exposure accounting for resting orders, cancel on breaker/suspension.
- `execution.maker.enabled: false`. Tests against the mock exchange.

### M2 — Maker entries for forecast trades (demo)
- Forecast single-leg entries go through the order manager when enabled.
- Fee model: `assume_taker` stays true for the EDGE decision until M0 shows
  what maker fills really cost; the actual fee paid is recorded per fill.
- Only where the market-skill gate already permits entry.

### M3 — Maker exits for non-urgent reasons (demo)
- Take-profit / edge-decayed / reduce rest for up to `exit_rest_seconds`,
  then fall back to taker. Urgent exits unchanged.

### M4 — Review and fee-model update
- Compare taker vs maker on realised P/L, fill rate, drift. Only then consider
  `assume_taker: false` for the entry decision.

---

## 5. Configuration (proposed, all off until signalled)

```yaml
execution:
  maker:
    enabled: false
    improve_cents: 1          # how far inside the bid to post
    min_edge_cents: 3         # never post a price that leaves less than this
    reprice_threshold_cents: 2
    max_reprices: 5
    max_rest_seconds: 900
    exit_rest_seconds: 300    # non-urgent exits, before falling back to taker
    poll_seconds: 3
maker_shadow:
  enabled: true              # M0 only; no orders
  drift_minutes: [5, 30, 60]
```

## 6. Tests to write

Pricing never crosses and never gives away the edge; re-price and cancel
rules; partial fills become positions; resting size counts toward exposure;
breaker/suspension cancels resting entries; startup cancels orphans; feed-down
cancels; urgent exits stay taker; arbitrage never rests; shadow simulation
against a recorded tape (fill / no fill / partial / drift).

## 7. Deployment (when signalled)

Same procedure as 14A and phases A-C: branch `package/c2`, phase by phase,
`tests/run_tests.py` 0 failed per phase, journal backup before any schema
change, `merge --ff-only`, restart, watch. M0 ships alone and runs 1-2 weeks
before M1 starts. Rollback: `execution.maker.enabled: false` (instant, no
restart of logic), or `git revert` + restart.

## 8. Open questions for the operator

1. Current Kalshi maker fee for the weather series - `fees.maker_rate` is
   0.0025 x C x P in config; verify against Kalshi's published schedule
   (some series charge no maker fee) before M2.
2. Is 1-2 weeks of shadow data (M0) acceptable before any real maker order?
3. Should maker entries be allowed only after the weather strategy is back in
   the market (phase B passing), or built in parallel so they are ready?
