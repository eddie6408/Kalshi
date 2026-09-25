> **Written 13 September 2026, and kept for the plain-English explanation of
> what a prediction market is and how the loop works — sections 0 and 1 are
> still accurate.**
>
> The mechanics have moved on since: the original engine was retired, the
> forecast strategy runs as parallel workers, trading is gated to six hours
> of lead time, and the self-test is 566 tests rather than 177. Where this
> guide and the bot disagree, the bot is right. `README.md` is the current
> short version.

# kalshibot — a Kalshi trading bot you actually own

Rendition 1. Paper-trading weather markets, with the live-trading path already
wired and locked behind two safety switches.

Read the four sections in order. Nothing here assumes you have written Python
before. Every command is meant to be copied and pasted exactly.

---

## 0. What this thing is, in plain English

A prediction market contract pays **$1.00** if something happens and **$0.00** if it
doesn't. If a contract trades at 61¢, the market is saying "61% chance."

That's the whole game. If you can work out the *real* probability more
accurately than the market can, and the gap is big enough to cover fees, you
make money over many trades. If you can't, you lose slowly and politely.

So the bot does exactly five things, in a loop, forever:

```
  1. SCAN      pull open markets from Kalshi
  2. ESTIMATE  work out the true probability from real data
  3. PRICE     is (our probability − market price) big enough to beat fees?
  4. APPROVE   does this pass every hard risk limit? (deterministic code, not opinion)
  5. EXECUTE   place the order, write everything down, manage the position
```

Step 2 is the only place an edge can come from. Steps 3 and 4 exist to stop a
good idea from turning into a blown account.

**Why weather first.** Kalshi's daily high-temperature markets settle on one
number: the official National Weather Service reading for one specific
station. Weather models publish *ensembles* — dozens of runs with slightly
different starting conditions — which give you a genuine probability
distribution for free. That means you can price every strike on the board
honestly, and you can check afterward whether you were right. Sports and
crypto markets are picked over by people with faster infrastructure than
yours. Weather is where a careful individual still has room.

---

## 0.5 Start here — watch it trade in two minutes

It is already installed at `~/trading/kalshi-bot`. Open **Terminal**
(`Cmd + Space`, type `Terminal`, Enter) and paste these three lines:

```bash
cd ~/trading/kalshi-bot
bash setup.sh
python scripts/simulate.py --slow
```

`setup.sh` builds the Python environment, installs everything and runs the
72-test suite. `simulate.py` then runs the **real** bot — real estimator, real
edge math, real risk engine — against a **fake** exchange and a **fake**
weather model. You will watch it price a board of temperature buckets, find
the mispriced ones, get told "no" by the risk engine, size what survives, take
positions, exit them, and settle.

No Kalshi account. No API key. No money. Nothing to lose and nothing to break.

**What you are watching is the machinery, not a backtest.** The simulation is
built so the bot's model is usually a little sharper than the simulated
market, because a demo where it has no edge would tell you nothing. Real
markets owe you no such thing. The P/L that scrolls past proves the code runs
correctly; it proves nothing about whether this makes money.

### Watch it live in a second window

While the simulation is running, open a **second** Terminal window and run:

```bash
cd ~/trading/kalshi-bot
source .venv/bin/activate
python scripts/monitor.py --db data/simulation.db
```

That is a live dashboard: open positions, running P/L, and the last dozen
decisions with our probability next to the market's. It refreshes every two
seconds. `Ctrl+C` stops watching without stopping the bot. The same command
with no `--db` watches a real run.

### Then read the report

```bash
python scripts/report.py --db data/simulation.db
```

The **CALIBRATION** table is the one that matters. Everything else is noise.

---

## 1. Setup for real trading — about 25 minutes

Section 0.5 already got the bot running. This section is about pointing it
at an actual Kalshi account.

### Step 1.0 — Terminal, not IDLE

Every command in this guide goes into **Terminal**, the built-in macOS command
line. Press `Cmd + Space`, type `Terminal`, press Enter. You'll get a window
with a text prompt ending in `%` or `$`.

**Do not use IDLE**, and don't use the "Run" button in an editor. IDLE is a
Python *shell* — it expects Python statements like `print("hi")`. Everything
below is a *shell* command: `cd`, `pip`, `python3 run.py`. Type
`mkdir -p ~/trading` into IDLE and you get a `SyntaxError`, which looks like
you broke something when you only used the wrong window.

Rule of thumb for the rest of this guide: **if it's in a grey code block
labelled `bash`, it goes in Terminal.** You never need to open IDLE at all.
You also never need an IDE — but if you want to *read* the code, VS Code or
even TextEdit is fine. Just don't run it from there.

To paste into Terminal, use `Cmd + V` and press Enter. Paste one line at a
time until you're comfortable; lines starting with `#` are comments and do
nothing, so you can skip them.

### Step 1.1 — Check you have Python

In Terminal, type this and press Enter:

```bash
python3 --version
```

You should see something like `Python 3.12.4`. You need 3.10 or newer. If it
says 3.9 or "command not found", install from
<https://www.python.org/downloads/>, then quit Terminal completely
(`Cmd + Q`) and reopen it before trying again.

### Step 1.2 — Unzip the project and move into its folder

You downloaded `kalshi-bot.zip`. It's almost certainly in your Downloads
folder. Type these four lines into Terminal, one at a time, pressing Enter
after each:

```bash
mkdir -p ~/trading
cd ~/trading
unzip ~/Downloads/kalshi-bot.zip
cd kalshi-bot
```

What each line did:

| Line | What it does |
|---|---|
| `mkdir -p ~/trading` | creates a folder called `trading` in your home directory |
| `cd ~/trading` | moves Terminal into that folder |
| `unzip ~/Downloads/kalshi-bot.zip` | unpacks the project there |
| `cd kalshi-bot` | moves Terminal into the unpacked project |

`~` is shorthand for your home folder, `/Users/eddiegutierrez`. `cd` means
"change directory" — it's how you tell Terminal *where* to run things.

Now confirm you're in the right place:

```bash
pwd
ls
```

`pwd` should print `/Users/eddiegutierrez/trading/kalshi-bot`, and `ls` should
list `README.md`, `config.yaml`, `run.py`, `kalshibot`, `scripts`, `tests`.
If you see those six things, you're good.

**If `unzip` says "cannot find or open"**, the file isn't where you expected.
Find it by double-clicking the zip in Finder instead — macOS will unpack it
into a `kalshi-bot` folder next to the zip. Then drag that folder into
`~/trading` in Finder and run `cd ~/trading/kalshi-bot`.

> **A shortcut worth knowing:** in Terminal you can type `cd ` (with a space)
> and then drag a folder from Finder onto the Terminal window. It fills in the
> full path for you. This works any time a command needs a path.

From now on, **every command in this guide assumes you are in that folder.**
If you close Terminal and open a fresh window later, the first thing you type
is always:

```bash
cd ~/trading/kalshi-bot
source .venv/bin/activate
```

(That second line won't work until you've done Step 1.3 once.)

### Step 1.3 — Create an isolated Python environment

A "virtual environment" is a private box of Python libraries that belongs to
this project only. It stops the bot's dependencies from colliding with
anything else on your Mac, and it means you can delete the whole thing later
without leaving a mess.

`bash setup.sh` does this step and the next one for you. If it worked, skip
ahead to Step 1.4. The manual version, for when you want to know what happened:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Your Terminal prompt should now start with `(.venv)`, like this:

```
(.venv) eddiegutierrez@MacBook kalshi-bot %
```

**That `(.venv)` prefix has to be there every time you run the bot.** If you
open a new Terminal window it will be missing — that's normal, just run
`source .venv/bin/activate` again after `cd`-ing into the folder.

Now install the libraries (this takes a minute and prints a lot of text —
that's fine):

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

When it finishes, prove the code is intact before touching any credentials:

```bash
python tests/run_tests.py
```

You want `84 passed, 0 failed` at the bottom. This needs no internet, no
Kalshi account, and no API key — if it passes, the math and the risk limits
are working.

### Step 1.4 — Create a Kalshi DEMO account first

Not your real account. The demo environment is a full copy of Kalshi with fake
money, and its credentials are completely separate from production.

1. Go to <https://demo.kalshi.co> and sign up.
2. Log in, open **Profile → Settings**.
3. Find the **API Keys** section and click **Create New API Key**.
4. You get two things:
   - a **Key ID** (looks like `a1b2c3d4-...`)
   - a **private key** file (starts with `-----BEGIN RSA PRIVATE KEY-----`)

> **The private key is shown exactly once.** Download it or copy it into a file
> before you close that page. If you lose it, delete the key and make a new one.

Save the private key somewhere only you can read:

```bash
mkdir -p ~/.kalshi
nano ~/.kalshi/kalshi_demo_key.pem
```

`nano` is a text editor that runs *inside* Terminal — the window won't change,
but you're now editing a file. Paste the whole private key (`Cmd + V`),
including the `-----BEGIN RSA PRIVATE KEY-----` and `-----END RSA PRIVATE
KEY-----` lines. Then:

- `Ctrl + O` (letter O, not zero), press Enter — saves
- `Ctrl + X` — exits back to the normal prompt

Then lock the file down so only you can read it:

```bash
chmod 600 ~/.kalshi/kalshi_demo_key.pem
```

### Step 1.5 — Tell the bot about your key

```bash
cp .env.example .env
nano .env
```

Fill in three lines:

```
KALSHI_KEY_ID=paste-your-key-id-here
KALSHI_PRIVATE_KEY_PATH=/Users/YOURNAME/.kalshi/kalshi_demo_key.pem
KALSHI_ENV=demo
```

Use the **full** path, starting with `/Users/`. `~` will not work here.
Save with `Ctrl+O`, Enter, then `Ctrl+X`.

`.env` is already in `.gitignore`. Never paste its contents anywhere.

### Step 1.6 — Prove it works

```bash
python scripts/check_connection.py
```

You want a wall of `[OK]`. The last line should show your demo balance.

If something fails, the script tells you the three usual causes. The most
common by far is a **production key with `KALSHI_ENV=demo`** (or the reverse) —
they are genuinely different accounts and the keys are not interchangeable.

---

## 2. Your first run

```bash
python run.py --once
```

This does one full scan and exits. You'll see a line per market:

```
KXHIGHNY-26SEP13-B72.5   ours=0.418 mkt(yes ask)=0.200  best=yes@20c edge=+0.206 conf=0.74
  -> no trade: edge +0.206 ... kelly suggests 142; bankroll-fraction cap caps to 28
```

Read that left to right:

| Field | Meaning |
|---|---|
| `ours=0.418` | our model says 41.8% |
| `mkt=0.200` | the market is charging 20¢, i.e. pricing it at 20% |
| `edge=+0.206` | 20.6 points of edge after fees |
| `conf=0.74` | how much we trust our own number |
| `kelly suggests 142` | the math wants 142 contracts |
| `caps to 28` | risk limits cut it to 28 |

That last line is the whole point of the design. The model gets to have an
opinion; the risk engine decides what actually happens.

Now let it run continuously:

```bash
python run.py
```

Press `Ctrl+C` to stop. In **paper** mode it never sends an order to Kalshi —
it reads real prices and simulates fills at the real ask.

If you see `no open markets found`, the ticker prefixes in `config.yaml` have
changed. Find the current ones:

```bash
python scripts/find_series.py temperature
```

Then edit the `ticker_prefix` values under `weather.series` in `config.yaml`.

---

## 3. How the code is laid out

```
kalshi-bot/
├── setup.sh                    one-command installer
├── run.py                      entry point
├── config.yaml                 every number the bot obeys  <-- you edit this
├── .env                        secrets only                <-- never share
│
├── kalshibot/
│   ├── config.py               loads + validates config, refuses unsafe combos
│   ├── auth.py                 RSA-PSS request signing
│   ├── client.py               REST client: throttling, retries, pagination
│   ├── models.py               Market, Orderbook, Position, TradeIntent
│   │
│   ├── estimators/             "what is the true probability?"
│   │   ├── base.py             the interface every estimator implements
│   │   ├── weather.py          ensemble forecast -> probability per strike
│   │   └── llm.py              Claude estimator, scaffolded for rendition 2
│   │
│   ├── weatherdata.py          Open-Meteo ensemble + NWS, both free
│   ├── fees.py                 Kalshi's fee curve
│   ├── edge.py                 expected value and Kelly sizing
│   ├── risk.py                 the veto. hard limits, no judgement calls
│   ├── execution.py            paper / demo / live order routing
│   ├── engine.py               the loop that ties it together
│   ├── simulate.py             fake exchange + fake forecasts, for the demo
│   └── journal.py              SQLite record of every decision
│
├── scripts/
│   ├── simulate.py             watch it trade with no account
│   ├── monitor.py              live dashboard
│   ├── check_connection.py     run this first
│   ├── find_series.py          discover current Kalshi ticker names
│   ├── settle.py               score past forecasts against reality
│   └── report.py               P/L and calibration
│
└── tests/                      84 tests, no network needed
```

**The one architectural rule:** the estimator never sees money and the risk
engine never sees a forecast. An estimator returns a probability and a
confidence, full stop. That separation is why you can swap a weather model for
a Claude-powered estimator later without touching a single risk limit — and
why a bug in one can't quietly become a bug in the other.

### The four numbers that control everything

In `config.yaml`:

```yaml
execution:
  mode: paper                     # paper | demo | live

risk:
  max_cost_per_trade_usd: 50.0    # ceiling on one order
  max_fraction_of_bankroll_at_risk: 0.02
  kelly_fraction: 0.25            # quarter Kelly

edge:
  min_edge_after_fees: 0.05       # need 5 points to bother
```

On **Kelly**: full Kelly is the mathematically optimal bet size *if your
probabilities are exactly right*. Yours aren't. Quarter Kelly gives up a
little growth for a very large reduction in the chance of a deep drawdown.
Do not raise this to 1.0 because a backtest looked good.

On **fees**: Kalshi charges roughly `0.07 × contracts × P × (1−P)`, rounded up
to the cent. That peaks at 50¢ — about 1.75¢ per contract — and shrinks toward
the extremes. A coin-flip market is the most expensive trade on the board, and
a 3-point "edge" at 50¢ is not an edge at all once you pay to get in and out.
That's why `min_edge_after_fees` starts at 5 points. Verify the current rates
on Kalshi's fee schedule and update `config.yaml` if they've moved.

### Run the tests any time you change something

```bash
python tests/run_tests.py
```

84 tests, no network, no credentials. They cover the fee curve, expected
value, Kelly sizing, every risk limit, strike parsing, the probability math,
and one end-to-end run against a fake exchange. If you edit `risk.py` or
`edge.py`, run these before you run the bot.

---

## 4. The path to real money

Do not skip steps. Each one exists because of a specific way people lose money.

### Stage 1 — Paper, two weeks minimum

Leave `mode: paper`. Let it run. Then, every few days:

```bash
python scripts/settle.py     # fill in what the temperature actually was
python scripts/report.py     # how did we do?
```

The section that matters is **CALIBRATION**:

```
  bucket          n   predicted    actual     net P/L
  0.6-0.8        41       0.687     0.683      +18.40
```

When you said 68.7%, it happened 68.3% of the time. That is a bot with an
edge. If your 70% bucket comes in at 45%, you do not have an edge — you have
an overconfident model, and position sizing cannot fix that. Fix the model.

Also watch **FORECAST ERROR**. If the mean absolute error is well above your
configured `station_representativeness_sigma_f`, raise it. If there's a
consistent bias of more than a degree, the model runs warm or cold at that
station and you should correct for it. This is the feedback loop that makes
the bot get better instead of just busier.

**Do not move on until you have at least 50 settled positions and honest
calibration.** Two weeks is a floor, not a target.

### Stage 2 — Kalshi demo, two more weeks

```yaml
execution:
  mode: demo
```

Keep `KALSHI_ENV=demo`. Now real orders go to Kalshi's sandbox with fake
money. This is where you find out that your simulated fills were optimistic,
that partial fills happen, and that the order API rejects things you didn't
expect. Better to learn that now.

### Stage 3 — Live, small

Three separate things must change, on purpose:

```bash
# in .env
KALSHI_ENV=prod
KALSHI_KEY_ID=<a NEW key, created on your real kalshi.com account>
KALSHI_PRIVATE_KEY_PATH=/Users/YOURNAME/.kalshi/kalshi_prod_key.pem
```

```yaml
# in config.yaml
execution:
  mode: live
safety:
  i_understand_this_trades_real_money: true
```

The bot refuses to start if `mode: live` and that flag is false, and it
refuses if `mode: live` with `KALSHI_ENV=demo`. Those guards are there so a
half-finished edit can't go live by accident.

**Start with limits an order of magnitude below what you think you want:**

```yaml
risk:
  bankroll_usd: 200.0
  max_cost_per_trade_usd: 5.0
  max_total_open_exposure_usd: 50.0
safety:
  daily_loss_limit_usd: 10.0
```

Run it for a month at that size. If the calibration holds with real fills and
real slippage, scale up gradually. If it doesn't, you found out for $10.

### The circuit breakers

The bot stops itself and cancels resting orders when:

- account balance drops below `safety.min_account_balance_usd`
- the day's P/L hits `safety.daily_loss_limit_usd`
- `safety.max_consecutive_losses` trades lose in a row

It does not restart on its own. That's deliberate — a bot that trips a limit
should get a human looking at it.

---

## 5. Running it 24/7

### On your Mac (development and paper trading)

```bash
cd ~/trading/kalshi-bot
source .venv/bin/activate
nohup python run.py > logs/nohup.log 2>&1 &
```

Watch it: `tail -f logs/kalshibot.log`. Stop it: `pkill -f "python run.py"`.

This stops when the machine sleeps. Fine for paper trading, not for live.

### On a small Linux VPS (what you actually want)

Any $5–12/month box works — DigitalOcean, Hetzner, Vultr, a t4g.micro.
Kalshi is US-only, so **pick a US region**.

```bash
ssh root@YOUR_SERVER_IP
adduser --disabled-password --gecos "" kalshi
apt update && apt install -y python3-venv python3-pip git
su - kalshi

# copy the project up from your Mac first, in another Terminal window:
#   scp -r ~/trading/kalshi-bot kalshi@YOUR_SERVER_IP:~/
#   scp ~/.kalshi/kalshi_prod_key.pem kalshi@YOUR_SERVER_IP:~/.kalshi/

cd ~/kalshi-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
nano .env          # fix KALSHI_PRIVATE_KEY_PATH to /home/kalshi/.kalshi/...
chmod 600 ~/.kalshi/*.pem
python scripts/check_connection.py
```

Then install it as a service so it restarts on reboot. `exit` back to root:

```bash
cp /home/kalshi/kalshi-bot/deploy/kalshibot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now kalshibot
systemctl status kalshibot
journalctl -u kalshibot -f      # live logs
```

And a daily settlement job so the calibration report stays current:

```bash
crontab -u kalshi -e
# add this line:
15 9 * * * cd /home/kalshi/kalshi-bot && .venv/bin/python scripts/settle.py >> logs/settle.log 2>&1
```

---

## 6. Rendition 2 — where this goes next

Roughly in order of value per hour spent:

1. **Tune from your own journal.** After a few hundred forecasts, fit
   `station_representativeness_sigma_f` and any per-station bias from the
   `forecasts` table instead of guessing. This is the highest-value change and
   it costs nothing.
2. **Backtest properly.** Replay historical ensemble forecasts against
   historical Kalshi prices. Right now you're forward-testing, which is honest
   but slow.
3. **WebSocket market data.** Replace REST polling with the `orderbook_delta`
   channel so you see price moves in milliseconds instead of minutes.
4. **Maker orders.** Post inside the spread with `post_only: true` instead of
   crossing it. The fee difference is large and compounds.
5. **Turn on the Claude estimator** (`kalshibot/estimators/llm.py`). This is
   what makes the news-driven markets in your brief reachable — the ones no
   formula can price. It's scaffolded and documented; it needs an
   `ANTHROPIC_API_KEY`, a function that gathers evidence for a given market,
   and registration in `app.py`. Keep the rule that it only ever returns a
   probability and a confidence.
6. **Add a second estimator** and only trade when they agree. Disagreement
   between independent methods is the cheapest confidence signal there is.

---

## 7. Things worth knowing before you fund this

- **Most retail prediction-market bots lose money.** The fee curve is a real
  headwind and the obvious edges are taken. Treat this as a research project
  with a defined budget, not an income stream.
- **Only trade money you can lose entirely.** Kalshi is a regulated exchange,
  but a bug in your own code is not covered by anything.
- **You are responsible for the taxes and for the rules that apply to you.**
  Kalshi is US-only and restricted in some states. If your employment or
  service imposes restrictions on outside financial activity or automated
  trading, check before you fund a live account, not after.
- **Nothing in this repository is financial advice.** It is a tool that does
  what you configure it to do.

---

## Quick reference

All of these go in **Terminal**, never IDLE. Start every session with the
first two lines.

```bash
cd ~/trading/kalshi-bot             # get into the project
source .venv/bin/activate           # always second

bash setup.sh                       # one-command install (safe to re-run)
python scripts/simulate.py --slow   # watch it trade, no account needed
python scripts/monitor.py           # live dashboard (second window)
python scripts/report.py            # P/L + calibration

python scripts/check_connection.py  # verify your Kalshi credentials
python run.py --once                # one scan, then exit
python run.py                       # run the loop
python run.py --dry-run             # force paper regardless of config
python scripts/find_series.py temperature   # find current tickers
python scripts/settle.py            # score past forecasts
python scripts/report.py            # P/L + calibration
python tests/run_tests.py           # 84 tests, no network
tail -f logs/kalshibot.log          # watch it think
```
