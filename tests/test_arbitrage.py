"""Structural arbitrage. These trades claim to be risk-free, so every claim
is checked against EVERY resolution outcome, not the convenient one.

All pricing goes through real order-book depth, so the fixtures are books.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshibot.depth import DepthBook, Level
from kalshibot.fees import FeeModel
from kalshibot.models import Market
from kalshibot.opportunity import ExecutionRisk
from kalshibot.strategies.arbitrage import (ArbConfig, ExclusiveBasketArbitrage,
                                            LadderInversionArbitrage,
                                            SameMarketArbitrage,
                                            StructuralArbitrageScanner)

FEES = FeeModel({"safety_multiplier": 1.0})
SOON = datetime.now(timezone.utc) + timedelta(hours=6)


def cfg(**kw):
    base = dict(max_contracts_per_trade=500, min_net_edge_per_contract=0.001)
    base.update(kw)
    return ArbConfig.from_dict(base)


def book(ticker, yes_ask=None, yes_ask_qty=0, no_ask=None, no_ask_qty=0,
         yes_bid=None, yes_bid_qty=0, no_bid=None, no_bid_qty=0, extra_yes=(), extra_no=()):
    """Build a book from the ASK side you want (asks are derived from opposite bids)."""
    yes_bids, no_bids = [], []
    if no_ask is not None:            # NO offered at X == YES bid at 100-X
        yes_bids.append(Level(100 - no_ask, no_ask_qty))
    if yes_bid is not None:
        yes_bids.append(Level(yes_bid, yes_bid_qty))
    if yes_ask is not None:           # YES offered at X == NO bid at 100-X
        no_bids.append(Level(100 - yes_ask, yes_ask_qty))
    if no_bid is not None:
        no_bids.append(Level(no_bid, no_bid_qty))
    yes_bids += [Level(p, q) for p, q in extra_yes]
    no_bids += [Level(p, q) for p, q in extra_no]
    return DepthBook(ticker, yes_bids, no_bids)


def mkt(ticker, event="EV", **kw):
    d = dict(ticker=ticker, event_ticker=event, series_ticker="S",
             status="open", close_time=SOON)
    d.update(kw)
    return Market(**d)


# ===================== TYPE A: same market =====================
def test_same_market_arbitrage():
    # YES ask 40c, NO ask 52c -> 92c for a certain $1.00. 8c of gross edge,
    # which clears the ~3.4c of two-leg fees near the middle of the curve.
    b = book("T", yes_ask=40, yes_ask_qty=100, no_ask=52, no_ask_qty=100)
    ops = SameMarketArbitrage(FEES, cfg()).find([mkt("T")], {"T": b})
    assert len(ops) == 1
    op = ops[0]
    assert op.strategy_type == "same_market"
    assert op.guaranteed
    assert op.guaranteed_min_payout == op.quantity * 1.00
    assert op.net_edge > 0


def test_same_market_no_arbitrage_after_fees():
    # 99c for $1.00 is 1c gross; two legs of fees cost more than that.
    b = book("T", yes_ask=50, yes_ask_qty=100, no_ask=49, no_ask_qty=100)
    assert SameMarketArbitrage(FEES, cfg()).find([mkt("T")], {"T": b}) == []


def test_same_market_absent_when_sides_sum_over_a_dollar():
    b = book("T", yes_ask=48, yes_ask_qty=100, no_ask=55, no_ask_qty=100)
    assert SameMarketArbitrage(FEES, cfg()).find([mkt("T")], {"T": b}) == []


def test_same_market_payout_identical_in_both_worlds():
    b = book("T", yes_ask=40, yes_ask_qty=100, no_ask=52, no_ask_qty=100)
    op = SameMarketArbitrage(FEES, cfg()).find([mkt("T")], {"T": b})[0]
    # YES resolves: yes leg pays 1, no leg pays 0. NO resolves: mirror. Always $1.
    assert op.guaranteed_min_payout == op.max_payout


# ===================== ORDER BOOK DEPTH =====================
def test_orderbook_depth_changes_true_cost():
    """The spec's worked example. 15 contracts do NOT all cost the best ask."""
    b = DepthBook("T",
                  yes_bids=[Level(45, 15), Level(44, 15)],   # -> NO asks 55, 56
                  no_bids=[Level(58, 10), Level(57, 20)])    # -> YES asks 42, 43
    w = b.cost_to_buy("yes", 15)
    assert w.filled == 15
    assert w.total_cost_cents == 10 * 42 + 5 * 43 == 635
    assert w.total_cost_cents != 15 * 42, "must not use top-of-book for the whole size"
    assert abs(w.vwap_cents - 635 / 15) < 1e-9


def test_depth_walk_reports_partial_availability():
    b = DepthBook("T", yes_bids=[], no_bids=[Level(58, 10)])
    w = b.cost_to_buy("yes", 500)
    assert w.filled == 10 and not w.complete


def test_buy_and_sell_consume_different_sides():
    b = DepthBook("T", yes_bids=[Level(60, 100)], no_bids=[Level(38, 150)])
    assert b.best_price("yes", "buy") == 62      # from the NO bid
    assert b.best_price("yes", "sell") == 60     # the YES bid itself
    assert b.best_price("yes", "buy") != b.best_price("yes", "sell")


def test_zero_quantity_rejected():
    b = DepthBook("T", yes_bids=[Level(60, 100)], no_bids=[Level(38, 150)])
    w = b.walk("yes", "buy", 0)
    assert w.filled == 0 and w.total_cost_cents == 0


def test_scanner_prices_at_depth_not_top_of_book():
    """Only 5 contracts at the good price, thousands at a bad one. The
    opportunity must be sized to the good price, not the whole book."""
    b = DepthBook("T",
                  yes_bids=[Level(48, 5), Level(20, 5000)],   # NO asks 52 then 80
                  no_bids=[Level(60, 5), Level(20, 5000)])    # YES asks 40 then 80
    ops = SameMarketArbitrage(FEES, cfg()).find([mkt("T")], {"T": b})
    assert ops, "a 5-lot arb exists"
    assert ops[0].quantity <= 5, f"sized {ops[0].quantity}, only 5 are cheap"


# ===================== TYPE B: ladder =====================
#: Real Kalshi ladders name the same subject at two thresholds. "B" is the
#: subject here; 70 and 75 are the rungs.
LOW_T, HIGH_T = "EV-B70", "EV-B75"


def _rungs(low_ask, low_qty, high_no_ask, high_qty, **over):
    low = mkt(LOW_T, strike_type="greater", floor_strike=70,
              subtitle="70 or above", **over)
    high = mkt(HIGH_T, strike_type="greater", floor_strike=75,
               subtitle="75 or above", **over)
    lb = book(LOW_T, yes_ask=low_ask, yes_ask_qty=low_qty)
    hb = book(HIGH_T, no_ask=high_no_ask, no_ask_qty=high_qty)
    return [low, high], {LOW_T: lb, HIGH_T: hb}


def test_ladder_direction():
    """BUY the lower threshold, BUY NO on the higher one."""
    ms, bs = _rungs(low_ask=30, low_qty=100, high_no_ask=55, high_qty=100)
    ops = LadderInversionArbitrage(FEES, cfg()).find(ms, bs)
    assert len(ops) == 1
    op = ops[0]
    by = {l.ticker: l for l in op.legs}
    assert by[LOW_T].side == "yes", "must BUY YES on the LOWER threshold"
    assert by[HIGH_T].side == "no", "must BUY NO on the HIGHER threshold"
    assert op.guaranteed_min_payout == op.quantity * 1.00
    assert op.max_payout == op.quantity * 2.00


def test_reversed_ladder_trade_can_pay_nothing():
    """The direction that looks symmetrical and is not.
    Buy the HIGHER rung, sell the lower one, land between them: pays ZERO."""
    payouts = []
    for temp in (80, 72, 65):
        high_pays = 1.0 if temp >= 75 else 0.0
        no_on_low_pays = 0.0 if temp >= 70 else 1.0
        payouts.append(high_pays + no_on_low_pays)
    assert min(payouts) == 0.0, "the reversed trade has a zero-payout world"
    # and the correct direction never does
    correct = []
    for temp in (80, 72, 65):
        low_pays = 1.0 if temp >= 70 else 0.0
        no_on_high_pays = 0.0 if temp >= 75 else 1.0
        correct.append(low_pays + no_on_high_pays)
    assert min(correct) == 1.0 and max(correct) == 2.0


def test_ladder_payoff_checked_across_all_three_regions():
    ms, bs = _rungs(low_ask=30, low_qty=100, high_no_ask=55, high_qty=100)
    op = LadderInversionArbitrage(FEES, cfg()).find(ms, bs)[0]
    per_set_cost = op.gross_cost / op.quantity
    for temp in (80, 72, 65):
        low_pays = 1.0 if temp >= 70 else 0.0
        no_on_high_pays = 0.0 if temp >= 75 else 1.0
        assert low_pays + no_on_high_pays >= per_set_cost, f"loses at T={temp}"


def test_ladder_requires_same_event():
    ms, bs = _rungs(low_ask=30, low_qty=100, high_no_ask=55, high_qty=100)
    ms[1].event_ticker = "DIFFERENT"
    assert LadderInversionArbitrage(FEES, cfg()).find(ms, bs) == []


def test_ladder_requires_compatible_settlement_times():
    ms, bs = _rungs(low_ask=30, low_qty=100, high_no_ask=55, high_qty=100)
    ms[1].close_time = SOON + timedelta(days=3)
    assert LadderInversionArbitrage(FEES, cfg()).find(ms, bs) == []


def test_ladder_not_inferred_from_ticker_similarity_alone():
    """Two markets whose names look related but whose strike types differ."""
    ms, bs = _rungs(low_ask=30, low_qty=100, high_no_ask=55, high_qty=100)
    ms[1].strike_type = "between"
    assert LadderInversionArbitrage(FEES, cfg()).find(ms, bs) == []


def test_ladder_refuses_opposing_outcomes_in_the_same_event():
    """The bug this test exists to prevent.

    A sports event holds every outcome of one contest, including outcomes that
    EXCLUDE each other. Kalshi's live board showed these two together:

        KXSERIEASPREAD-26SEP14TORROM-TOR2   Toronto wins by 2 or more
        KXSERIEASPREAD-26SEP14TORROM-ROM2   Roma wins by 2 or more

    Same event, same series, same strike type, same settlement time, and the
    threshold parser pulls "2" out of both. Everything the old compatible()
    checked said "ladder". It is not one - the two markets are about DIFFERENT
    TEAMS, and buying YES on one with NO on the other has a world that pays
    nothing at all.
    """
    ev = "KXSERIEASPREAD-26SEP14TORROM"
    tor = mkt(f"{ev}-TOR2", event=ev, strike_type="greater", floor_strike=2,
              subtitle="Toronto wins by 2 or more")
    rom = mkt(f"{ev}-ROM2", event=ev, strike_type="greater", floor_strike=2,
              subtitle="Roma wins by 2 or more")
    books = {tor.ticker: book(tor.ticker, yes_ask=6, yes_ask_qty=50),
             rom.ticker: book(rom.ticker, no_ask=64, no_ask_qty=50)}

    ok, why = LadderInversionArbitrage(FEES, cfg()).compatible(tor, rom)
    assert not ok and "subject" in why, why
    assert LadderInversionArbitrage(FEES, cfg()).find([tor, rom], books) == []


def test_opposing_outcome_pair_has_a_zero_payout_world():
    """Why the pair above is not arbitrage, checked outcome by outcome.
    Holding YES on 'Toronto by 2+' and NO on 'Roma by 2+':"""
    payouts = []
    for margin in (+3, 0, -3):          # Toronto by 3, a draw, Roma by 3
        yes_toronto_by_2 = 1.0 if margin >= 2 else 0.0
        no_roma_by_2 = 0.0 if margin <= -2 else 1.0
        payouts.append(yes_toronto_by_2 + no_roma_by_2)
    assert payouts == [2.0, 1.0, 0.0]
    assert min(payouts) == 0.0, "there is no $1.00 floor here"


def test_ladder_accepts_two_rungs_about_the_same_subject():
    """The same sports event DOES contain genuine ladders - two thresholds on
    the SAME team. Those must still be found."""
    ev = "KXNFLSPREAD-26SEP17DETBUF"
    low = mkt(f"{ev}-DET4", event=ev, strike_type="greater", floor_strike=4,
              subtitle="Detroit wins by 4 or more")
    high = mkt(f"{ev}-DET8", event=ev, strike_type="greater", floor_strike=8,
               subtitle="Detroit wins by 8 or more")
    ok, why = LadderInversionArbitrage(FEES, cfg()).compatible(low, high)
    assert ok, why
    books = {low.ticker: book(low.ticker, yes_ask=30, yes_ask_qty=100),
             high.ticker: book(high.ticker, no_ask=55, no_ask_qty=100)}
    ops = LadderInversionArbitrage(FEES, cfg()).find([low, high], books)
    assert len(ops) == 1
    by = {l.ticker: l for l in ops[0].legs}
    assert by[low.ticker].side == "yes" and by[high.ticker].side == "no"


def test_ladder_fails_closed_when_subject_is_unproven():
    """No subject in the ticker and none in the text is not proof of sameness.
    Absence of evidence must refuse the trade, not allow it."""
    a = mkt("EV-1", strike_type="greater", floor_strike=70, subtitle="", title="")
    b = mkt("EV-2", strike_type="greater", floor_strike=75, subtitle="", title="")
    ok, why = LadderInversionArbitrage(FEES, cfg()).compatible(a, b)
    assert not ok and "unproven" in why, why


def test_every_ladder_opportunity_names_the_subject_it_proved():
    ms, bs = _rungs(low_ask=30, low_qty=100, high_no_ask=55, high_qty=100)
    op = LadderInversionArbitrage(FEES, cfg()).find(ms, bs)[0]
    assert op.evidence.get("subject"), "the proof must be recorded, not assumed"


def test_no_ladder_arb_when_prices_ordered_correctly():
    ms, bs = _rungs(low_ask=45, low_qty=100, high_no_ask=70, high_qty=100)
    assert LadderInversionArbitrage(FEES, cfg()).find(ms, bs) == []


# ===================== TYPE C: exclusive basket =====================
def _basket(no_asks, qty=200, event="EV"):
    ms, bs = [], {}
    for i, na in enumerate(no_asks):
        t = f"M{i}"
        ms.append(mkt(t, event=event))
        bs[t] = book(t, no_ask=na, no_ask_qty=qty)
    return ms, bs


def test_exclusive_basket():
    # 4 outcomes, NO costs 70c each -> $2.80 for a payout floor of $3.00
    ms, bs = _basket([70, 70, 70, 70])
    ops = ExclusiveBasketArbitrage(FEES, cfg()).find("EV", ms, bs, is_exclusive=True)
    assert len(ops) == 1
    op = ops[0]
    assert op.guaranteed_min_payout == op.quantity * 3.0   # k-1
    assert op.max_payout == op.quantity * 4.0              # k
    assert op.net_edge > 0


def test_basket_floor_assumes_at_most_one_winner_never_exactly_one():
    ms, bs = _basket([70, 70, 70, 70])
    op = ExclusiveBasketArbitrage(FEES, cfg()).find("EV", ms, bs, True)[0]
    k = len(ms)
    assert op.guaranteed_min_payout == op.quantity * (k - 1)
    assert op.guaranteed_min_payout < op.max_payout


def test_basket_requires_the_exchange_exclusivity_flag():
    ms, bs = _basket([70, 70, 70, 70])
    assert ExclusiveBasketArbitrage(FEES, cfg()).find("EV", ms, bs, is_exclusive=False) == []


def test_yes_basket_disabled_by_default():
    """'At most one wins' does not mean 'one must win', so buying YES on
    every outcome is not risk-free and must stay off."""
    assert ArbConfig.from_dict({}).allow_yes_basket is False
    assert ArbConfig.from_dict({"allow_yes_basket": True}).allow_yes_basket is True


def test_basket_absent_when_no_asks_sum_too_high():
    ms, bs = _basket([80, 80, 80, 80])   # $3.20 cost vs $3.00 floor
    assert ExclusiveBasketArbitrage(FEES, cfg()).find("EV", ms, bs, True) == []


def test_basket_respects_leg_ceiling():
    ms, bs = _basket([70] * 20)
    assert ExclusiveBasketArbitrage(FEES, cfg(max_legs=14)).find("EV", ms, bs, True) == []


# ===================== FEES AND QUANTITY =====================
def test_many_legs_at_tiny_size_are_killed_by_rounding():
    """12 legs, 8c of gross edge. Each leg rounds its fee up to a whole cent,
    so at one contract the fees alone exceed the edge."""
    ms, bs = _basket([91] * 12, qty=1000)     # cost $10.92 vs floor $11.00
    ops = ExclusiveBasketArbitrage(
        FEES, cfg(max_contracts_per_trade=1)).find("EV", ms, bs, True)
    assert ops == [], "12 legs at 1 contract cannot clear per-leg fee rounding"


def test_same_basket_clears_at_scale():
    ms, bs = _basket([91] * 12, qty=1000)
    ops = ExclusiveBasketArbitrage(
        FEES, cfg(max_contracts_per_trade=1000)).find("EV", ms, bs, True)
    assert ops, "the same structure must clear once fees amortize"
    assert ops[0].quantity > 1
    assert ops[0].min_profitable_quantity > 1


def test_quantity_analysis_reports_all_three_numbers():
    ms, bs = _basket([91] * 12, qty=1000)
    op = ExclusiveBasketArbitrage(
        FEES, cfg(max_contracts_per_trade=1000)).find("EV", ms, bs, True)[0]
    assert op.max_executable_quantity > 0
    assert op.min_profitable_quantity > 0
    assert op.optimal_quantity >= op.min_profitable_quantity
    assert op.fee_drag_pct >= 0


def test_fee_drag_is_reported_honestly():
    b = book("T", yes_ask=40, yes_ask_qty=100, no_ask=52, no_ask_qty=100)
    op = SameMarketArbitrage(FEES, cfg()).find([mkt("T")], {"T": b})[0]
    assert 0 < op.fee_drag_pct < 100
    assert abs(op.net_edge - (op.gross_edge - op.estimated_fees)) < 1e-6


# ===================== EXECUTION RISK =====================
def test_deep_two_leg_book_is_low_risk():
    b = book("T", yes_ask=40, yes_ask_qty=100000, no_ask=52, no_ask_qty=100000)
    op = SameMarketArbitrage(FEES, cfg(max_contracts_per_trade=2)).find(
        [mkt("T")], {"T": b})[0]
    assert op.execution_risk in (ExecutionRisk.LOW, ExecutionRisk.NONE)


def test_wide_basket_is_high_risk():
    ms, bs = _basket([70] * 10, qty=12)
    op = ExclusiveBasketArbitrage(FEES, cfg(max_contracts_per_trade=10)).find(
        "EV", ms, bs, True)[0]
    assert op.execution_risk == ExecutionRisk.HIGH


# ===================== SCANNER =====================
def test_scanner_finds_nothing_on_an_efficient_board():
    ms, bs = _basket([88] * 8)   # $7.04 cost vs $7.00 floor - no edge
    s = StructuralArbitrageScanner(FEES, {"max_contracts_per_trade": 500})
    assert s.scan_event("EV", ms, bs, is_exclusive=True) == []


def test_scanner_sorts_by_net_edge():
    b1 = book("A", yes_ask=40, yes_ask_qty=100, no_ask=50, no_ask_qty=100)
    b2 = book("B", yes_ask=46, yes_ask_qty=100, no_ask=50, no_ask_qty=100)
    s = StructuralArbitrageScanner(FEES, {"max_contracts_per_trade": 100,
                                          "min_net_edge_per_contract": 0.001})
    ops = s.scan_event("EV", [mkt("A"), mkt("B")], {"A": b1, "B": b2})
    assert len(ops) == 2
    assert ops[0].net_edge >= ops[1].net_edge


def test_markets_with_no_book_are_skipped():
    s = StructuralArbitrageScanner(FEES, {})
    assert s.scan_event("EV", [mkt("DEAD")], {"DEAD": DepthBook.empty("DEAD")}) == []


# ===================== the fee floor, made explicit =====================
def test_a_three_cent_same_market_gap_is_not_an_arbitrage():
    """The trap. YES 42c + NO 55c = 97c looks like 3c of free money. Two legs
    priced near the middle of the fee curve cost about 3.4c, so it loses.
    Anything under roughly 3.5c of gross edge here is a mirage."""
    b = book("T", yes_ask=42, yes_ask_qty=1000, no_ask=55, no_ask_qty=1000)
    assert SameMarketArbitrage(FEES, cfg()).find([mkt("T")], {"T": b}) == []


def test_the_breakeven_gap_for_a_mid_priced_pair():
    """Sweep the gap and find where a same-market pair actually starts to pay."""
    first_profitable = None
    for total in range(99, 85, -1):          # 99c down to 86c
        yes_ask = total // 2
        no_ask = total - yes_ask
        b = book("T", yes_ask=yes_ask, yes_ask_qty=1000,
                 no_ask=no_ask, no_ask_qty=1000)
        if SameMarketArbitrage(FEES, cfg()).find([mkt("T")], {"T": b}):
            first_profitable = total
            break
    assert first_profitable is not None, "should clear somewhere below 96c"
    assert first_profitable <= 96, f"cleared at {first_profitable}c, expected <= 96c"
    assert first_profitable >= 90, f"cleared at {first_profitable}c, suspiciously generous"


# ============ orderbook response formats (regression) ============
# Kalshi ships more than one orderbook shape. Parsing only one of them
# against a deployment that uses the other yields an empty book for EVERY
# market, which reads as a dead exchange. It cost a full live scan to find.

FP = {"orderbook_fp": {"yes_dollars": [["0.4200", "13.00"], ["0.4100", "10.00"]],
                       "no_dollars": [["0.5600", "17.00"], ["0.4500", "20.00"]]}}
LEGACY = {"orderbook": {"yes": [[42, 13], [41, 10]], "no": [[56, 17], [45, 20]]}}


def test_parses_the_current_dollar_string_format():
    b = DepthBook.from_api("T", FP)
    assert b.source_format == "dollars"
    assert [(l.price_cents, l.contracts) for l in b.yes_bids] == [(42, 13), (41, 10)]
    assert [(l.price_cents, l.contracts) for l in b.no_bids] == [(56, 17), (45, 20)]


def test_parses_the_legacy_integer_cent_format():
    b = DepthBook.from_api("T", LEGACY)
    assert b.source_format == "cents"
    assert [(l.price_cents, l.contracts) for l in b.yes_bids] == [(42, 13), (41, 10)]


def test_both_formats_produce_identical_books():
    a, b = DepthBook.from_api("T", FP), DepthBook.from_api("T", LEGACY)
    assert [(l.price_cents, l.contracts) for l in a.yes_bids] == \
           [(l.price_cents, l.contracts) for l in b.yes_bids]
    assert a.best_price("yes", "buy") == b.best_price("yes", "buy")
    assert a.best_price("no", "buy") == b.best_price("no", "buy")


def test_asks_are_derived_correctly_from_dollar_strings():
    """A NO bid at $0.56 is a YES offer at 44c."""
    b = DepthBook.from_api("T", FP)
    assert b.best_price("yes", "buy") == 44
    assert b.best_price("no", "buy") == 58


def test_arrays_at_the_top_level_are_handled():
    b = DepthBook.from_api("T", {"yes_dollars": [["0.3000", "5.00"]],
                                 "no_dollars": [["0.6500", "5.00"]]})
    assert b.best_price("yes", "buy") == 35


def test_a_quiet_market_is_not_reported_as_a_parse_failure():
    """A recognised shape with empty arrays means nobody is quoting. That is
    a market condition, not a bug. Reporting it as 'unrecognised' sent a real
    debugging session hunting a parser problem that did not exist."""
    b = DepthBook.from_api("T", {"orderbook_fp": {"yes_dollars": [], "no_dollars": []}})
    assert b.source_format == "dollars-empty"
    assert "dollars" in b.source_format, "the shape WAS understood"
    assert b.best_price("yes", "buy") is None


def test_an_unknown_shape_is_reported_as_unrecognised():
    b = DepthBook.from_api("T", {"some_new_key": {"bids": [[1, 2]]}})
    assert b.source_format == "unrecognised"


def test_a_live_book_reports_the_plain_format():
    b = DepthBook.from_api("T", FP)
    assert b.source_format == "dollars"
    assert "empty" not in b.source_format


def test_malformed_rows_are_skipped_not_fatal():
    b = DepthBook.from_api("T", {"orderbook_fp": {
        "yes_dollars": [["0.4200", "13.00"], ["bad", "x"], [None], ["1.50", "5"],
                        ["0.0000", "5"], ["0.3000", "0"]],
        "no_dollars": []}})
    assert [(l.price_cents, l.contracts) for l in b.yes_bids] == [(42, 13)]


def test_a_real_arbitrage_is_found_through_the_dollar_format():
    """End to end from the wire format the live exchange actually sends."""
    payload = {"orderbook_fp": {"yes_dollars": [["0.5500", "300.00"]],
                                "no_dollars": [["0.6500", "300.00"]]}}
    b = DepthBook.from_api("ARB", payload)
    assert b.best_price("yes", "buy") == 35 and b.best_price("no", "buy") == 45
    ops = SameMarketArbitrage(FEES, cfg()).find([mkt("ARB")], {"ARB": b})
    assert len(ops) == 1 and ops[0].net_edge > 0


# ===================== size cap neutrality =====================
def test_raising_the_size_cap_never_finds_fewer_opportunities():
    """Raising max_contracts_per_trade must only change HOW MUCH is traded,
    never WHETHER something is found. analyse() always samples 1 set, so a
    structure that qualified at a cap of 1 must still qualify at 25."""
    m = mkt("T")
    for depth in (1, 2, 3, 5, 25, 400):
        b = DepthBook("T", yes_bids=[Level(48, depth)], no_bids=[Level(60, depth)])
        small = SameMarketArbitrage(FEES, cfg(max_contracts_per_trade=1)).find([m], {"T": b})
        large = SameMarketArbitrage(FEES, cfg(max_contracts_per_trade=25)).find([m], {"T": b})
        assert len(large) == len(small), f"depth {depth}: cap changed the count"
        if small:
            assert large[0].quantity >= small[0].quantity
            assert large[0].net_edge >= small[0].net_edge


def test_a_bigger_cap_sizes_up_to_available_depth():
    m = mkt("T")
    b = DepthBook("T", yes_bids=[Level(48, 400)], no_bids=[Level(60, 400)])
    op = SameMarketArbitrage(FEES, cfg(max_contracts_per_trade=25)).find([m], {"T": b})[0]
    assert op.quantity == 25, "should use the whole cap when the book allows"


# ===================== near-miss visibility =====================
def test_an_uncrossed_market_is_recorded_as_a_near_miss():
    """A board with no arbitrage must still produce evidence that it was
    looked at. Silence and blindness look identical otherwise."""
    m = mkt("T")
    # YES ask 45 + NO ask 58 = 103c for a $1.00 pair: real, priced, 3c short.
    b = book("T", yes_ask=45, yes_ask_qty=100, no_ask=58, no_ask_qty=100)
    d = SameMarketArbitrage(FEES, cfg())
    assert d.find([m], {"T": b}) == []
    assert len(d.near_misses) == 1
    nm = d.near_misses[0]
    assert nm.cost_cents == 103 and nm.floor_cents == 100 and nm.gap_cents == 3
    assert "T" in nm.describe()


def test_a_crossed_market_is_not_a_near_miss():
    m = mkt("T")
    b = book("T", yes_ask=40, yes_ask_qty=100, no_ask=52, no_ask_qty=100)
    d = SameMarketArbitrage(FEES, cfg())
    assert d.find([m], {"T": b}), "this one crosses"
    assert d.near_misses == []


def test_uncrossed_ladders_are_recorded_too():
    ms, bs = _rungs(low_ask=45, low_qty=100, high_no_ask=70, high_qty=100)
    d = LadderInversionArbitrage(FEES, cfg())
    assert d.find(ms, bs) == []
    assert len(d.near_misses) == 1
    assert d.near_misses[0].gap_cents == 15      # 115c against a 100c floor


def test_scanner_reports_the_tightest_near_misses_first():
    scanner = StructuralArbitrageScanner(FEES, {"max_contracts_per_trade": 25})

    class Ev:
        event_ticker = "EV"
        mutually_exclusive = False
        markets = [mkt("A"), mkt("B"), mkt("C")]

    books = {"A": book("A", yes_ask=45, yes_ask_qty=50, no_ask=70, no_ask_qty=50),
             "B": book("B", yes_ask=45, yes_ask_qty=50, no_ask=58, no_ask_qty=50),
             "C": book("C", yes_ask=49, yes_ask_qty=50, no_ask=52, no_ask_qty=50)}
    assert scanner.scan([Ev()], books) == []
    gaps = [nm.gap_cents for nm in scanner.near_misses()]
    assert gaps == sorted(gaps), "tightest gap must come first"
    assert gaps[0] == 1, "C is 101c against a 100c floor"
    assert scanner.near_miss_count() == 3


def test_near_misses_are_cleared_between_scans():
    scanner = StructuralArbitrageScanner(FEES, {"max_contracts_per_trade": 25})

    class Ev:
        event_ticker = "EV"
        mutually_exclusive = False
        markets = [mkt("A")]

    books = {"A": book("A", yes_ask=45, yes_ask_qty=50, no_ask=70, no_ask_qty=50)}
    for _ in range(3):
        scanner.scan([Ev()], books)
    assert scanner.near_miss_count() == 1, "a stale scan must not accumulate"


# ===================== sizing within the risk tier =====================
def _thin_ladder(depth):
    """A real ladder arb whose book is only `depth` contracts deep."""
    low = mkt(LOW_T, strike_type="greater", floor_strike=70, subtitle="70 or above")
    high = mkt(HIGH_T, strike_type="greater", floor_strike=75, subtitle="75 or above")
    lb = book(LOW_T, yes_ask=35, yes_ask_qty=depth)
    hb = book(HIGH_T, no_ask=55, no_ask_qty=depth)
    return [low, high], {LOW_T: lb, HIGH_T: hb}


def test_size_steps_down_to_meet_the_execution_risk_limit():
    """The quantity analyser maximises net edge, which always wants the whole
    book - and that is the size with the WORST execution risk, because risk is
    graded on depth headroom. A live scan produced four real ladder arbs at 20
    sets, all refused as HIGH risk, so nothing traded at all."""
    ms, bs = _thin_ladder(depth=20)
    greedy = LadderInversionArbitrage(
        FEES, cfg(max_contracts_per_trade=25, respect_execution_risk=False)
    ).find(ms, bs)[0]
    assert greedy.quantity == 20
    assert greedy.execution_risk is ExecutionRisk.HIGH, "the rejected shape"

    sized = LadderInversionArbitrage(
        FEES, cfg(max_contracts_per_trade=25, respect_execution_risk=True,
                  max_execution_risk="medium")
    ).find(ms, bs)[0]
    assert sized.quantity < greedy.quantity
    assert ExecutionRisk(sized.execution_risk) is ExecutionRisk.MEDIUM
    assert sized.net_edge > 0, "a smaller real edge is still an edge"


def test_a_deep_book_is_not_sized_down_for_no_reason():
    ms, bs = _thin_ladder(depth=5000)
    op = LadderInversionArbitrage(
        FEES, cfg(max_contracts_per_trade=25, respect_execution_risk=True)
    ).find(ms, bs)[0]
    assert op.quantity == 25, "plenty of headroom; take the whole cap"


def test_an_unsalvageable_trade_is_still_reported_not_dropped():
    """Beyond four legs the risk tier comes from leg count alone, so no size
    can rescue a wide basket. It must still reach the gate and be refused out
    loud - dropping it silently would make it vanish from the scan report with
    no reason given, which is how the bot stops being readable."""
    ms, bs = _basket([70] * 10, qty=12)
    ops = ExclusiveBasketArbitrage(
        FEES, cfg(max_contracts_per_trade=10, respect_execution_risk=True,
                  max_execution_risk="medium")).find("EV", ms, bs, True)
    assert ops, "the opportunity must survive to be explained"
    assert ops[0].execution_risk is ExecutionRisk.HIGH
    from kalshibot.gate import ProfitabilityGate, Reject
    d = ProfitabilityGate({"max_execution_risk": "medium"}).evaluate(ops[0])
    assert d.reason is Reject.EXECUTION_RISK and not d.accepted


def test_sizing_down_never_turns_a_profit_into_a_loss():
    for depth in (3, 8, 20, 60, 200):
        ms, bs = _thin_ladder(depth=depth)
        for op in LadderInversionArbitrage(
                FEES, cfg(max_contracts_per_trade=25,
                          respect_execution_risk=True)).find(ms, bs):
            assert op.net_edge > 0, f"depth {depth} produced a losing trade"
            assert op.guaranteed_min_payout > op.gross_cost
