# Package 8 — Fee schedule from Kalshi, per series

**Status: IMPLEMENTED 2026-09-28** (operator: "do the ones that are due now").
Research brief section 13; audit finding G7 (fees hard-coded, unversioned).

## Measured before building
- Every series the bot has traded is `quadratic` x1 on demo: the published
  taker fee is 0.07 x C x P x (1-P), exactly `fees.taker_rate` in config.
- 120 of 122 demo fills were charged exactly that; the other 2 were resting
  (maker) fills on weather, charged $0 - `quadratic` has no maker fee.
- On production, NFL/NCAAF series are `quadratic_with_maker_fees`; weather
  is `quadratic`.

## What it does
- `kalshibot/fee_schedule.py`: reads each series' `fee_type`,
  `fee_multiplier` and scheduled `/series/fee_changes` from demo (where the
  bot's fees are charged), cached 6 h; a failed read keeps the last good one.
- Every opportunity records `fee_schedule_version` =
  `<config fee model>|kalshi:<SERIES>:<type>:x<mult>` per series.
- **Entries are refused** (ledger stage GATE, `FEE_SCHEDULE_UNCERTAIN`) on a
  series whose schedule is unknown, unreadable with no last good copy, or
  above the multiplier the model assumes. Exits are never gated. Nothing ever
  prices a fee BELOW config.yaml; a cheaper schedule changes nothing.
- A scheduled fee change is alerted once (ntfy, high).
- The maker shadow (C2 M0) uses the PRODUCTION series' maker fee (0 for
  weather) instead of config's `maker_rate`.
- `scripts/fee_check.py`: charged vs published, fill by fill.

## Effect on trading today
None: the published schedule equals the configured one for every series
traded. `fees.schedule.enforce: false` makes it record-only.
