"""Tennis Markov model: known values, symmetry, monotonicity, state handling."""
import unittest

from lvt.edge.tennis_model import (A, B, TennisState, game_win, implied_strength, p_match, p_set,
                                   tiebreak_win)


def start(server=A, best_of=3):
    return TennisState((0, 0), (0, 0), (0, 0), server, best_of)


class TestGame(unittest.TestCase):
    def test_known_values(self):
        self.assertAlmostEqual(game_win(0.6, 0, 0), 0.7357, places=3)          # textbook value
        self.assertAlmostEqual(game_win(0.5, 0, 0), 0.5, places=9)
        self.assertEqual(game_win(0.6, 4, 2), 1.0)
        self.assertAlmostEqual(game_win(0.6, 3, 3), 0.36 / 0.52, places=9)     # deuce closed form

    def test_advantage_and_score_order(self):
        p = 0.63
        self.assertGreater(game_win(p, 4, 3), game_win(p, 3, 3))               # advantage server
        self.assertLess(game_win(p, 3, 4), game_win(p, 3, 3))
        self.assertGreater(game_win(p, 2, 0), game_win(p, 0, 2))


class TestTiebreakSetMatch(unittest.TestCase):
    def test_symmetry(self):
        self.assertAlmostEqual(tiebreak_win(0.62, 0.62, 0, 0, A), 0.5, places=6)
        self.assertAlmostEqual(p_match(0.62, 0.62, start()), 0.5, places=6)
        self.assertAlmostEqual(p_match(0.62, 0.62, start(best_of=5)), 0.5, places=6)

    def test_monotone_in_strength(self):
        vals = [p_match(0.60 + d, 0.62, start()) for d in (0.0, 0.02, 0.04, 0.06)]
        self.assertEqual(vals, sorted(vals))
        self.assertGreater(p_match(0.66, 0.60, start(best_of=5)), p_match(0.66, 0.60, start(best_of=3)))

    def test_score_states(self):
        pa = pb = 0.63
        up_break = TennisState((0, 0), (5, 3), (0, 0), A)
        self.assertGreater(p_set(pa, pb, up_break), 0.9)
        down_break = TennisState((0, 0), (3, 5), (0, 0), B)
        self.assertLess(p_set(pa, pb, down_break), 0.1)
        one_set_up = TennisState((1, 0), (0, 0), (0, 0), A)
        self.assertAlmostEqual(p_match(pa, pb, one_set_up), 0.75, delta=0.01)   # needs 1 of the next 2 sets
        won = TennisState((2, 0), (0, 0), (0, 0), A)
        self.assertEqual(p_match(pa, pb, won), 1.0)

    def test_break_point_matters(self):
        pa = pb = 0.63
        s = TennisState((0, 0), (3, 3), (0, 0), A)
        bp = TennisState((0, 0), (3, 3), (0, 3), A)            # 0-40 on A's serve
        self.assertLess(p_match(pa, pb, bp), p_match(pa, pb, s))

    def test_tiebreak_state(self):
        tb = TennisState((0, 0), (6, 6), (6, 2), A, in_tiebreak=True, tb_first=A)
        self.assertGreater(p_set(0.63, 0.63, tb), 0.95)

    def test_implied_strength_inverts_the_model(self):
        st = TennisState((0, 1), (2, 1), (1, 2), B)
        d = implied_strength(0.37, st, 0.63)
        self.assertAlmostEqual(p_match(0.63 + d / 2, 0.63 - d / 2, st), 0.37, places=3)


if __name__ == "__main__":
    unittest.main()
