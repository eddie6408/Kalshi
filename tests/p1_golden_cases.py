"""Fixed inputs for the Package 1 regression test.

The same books and markets are run through the structural scanner and the
forecast strategy, and each resulting opportunity is reduced to the numbers
that drive a trading decision. p1_golden.json holds those numbers as the code
produced them BEFORE Package 1; the test requires them to be unchanged after.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from kalshibot.gate import ProfitabilityGate
from kalshibot.strategies.arbitrage import StructuralArbitrageScanner

import test_arbitrage as ta
import test_forecast as tf


def _digest(op, gate):
    d = gate.evaluate(op)
    return {"fingerprint": op.fingerprint(), "quantity": op.quantity,
            "gross_cost": round(op.gross_cost, 6),
            "fees": round(op.estimated_fees, 6), "net_edge": round(op.net_edge, 6),
            "floor": round(op.guaranteed_min_payout, 6),
            "execution_risk": op.execution_risk.value,
            "accepted": d.accepted, "reason": d.reason.value}


def cases():
    out = {}
    scanner = StructuralArbitrageScanner(ta.FEES, {"max_contracts_per_trade": 500,
                                                   "min_net_edge_per_contract": 0.001})
    arb_gate = ProfitabilityGate({})
    structural = {
        "same_market": ([ta.mkt("T")], {"T": ta.book("T", yes_ask=40, yes_ask_qty=100,
                                                     no_ask=52, no_ask_qty=100)}, False),
        "same_market_deep": ([ta.mkt("T")], {"T": ta.book("T", yes_ask=40, yes_ask_qty=10,
                              no_ask=52, no_ask_qty=100, extra_yes=((44, 50),),
                              extra_no=((57, 60),))}, False),
        "ladder": ta._rungs(low_ask=30, low_qty=100, high_no_ask=55, high_qty=100) + (False,),
        "basket": ta._basket([70, 70, 70, 70]) + (True,),
    }
    for name, (ms, bs, excl) in structural.items():
        ops = scanner.scan_event("EV", ms, bs, is_exclusive=excl)
        out[name] = [_digest(o, arb_gate) for o in sorted(ops, key=lambda o: o.fingerprint())]

    fc_gate = ProfitabilityGate({"allow_forecast": True, "require_guaranteed": False})
    for name, est, bk in (("forecast_72", tf.FakeEstimator(0.72, 0.9), tf.book(61)),
                          ("forecast_no_side", tf.FakeEstimator(0.20, 0.9), tf.book(45))):
        s = tf.ForecastStrategy(est, tf.FEES, tf.cfg())
        ops = s.find([tf.mkt()], {"KXFAKE-1": bk}, 500.0)
        out[name] = [_digest(o, fc_gate) for o in ops]
    return out


if __name__ == "__main__":
    import json
    print(json.dumps(cases(), indent=1, sort_keys=True))
