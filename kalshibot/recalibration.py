"""Recalibrate a model's probabilities from its own settled record.

Why (2026-10-10 health check): the same-day nowcast was UNDER-confident.
Over 171 settled contracts, what it called 65-85% happened 95-100% of the
time and what it called 15-35% happened 0-17%. Its distributions are too
wide, so it saw value in cheap long shots - the live paper account lost $119
on 74 such trades where it expected +$221.

The correction is a logistic map fitted per market kind (daily HIGH / LOW):

    p' = sigmoid(a + b*logit(p) + c*B + d*B*logit(p))       B = 1 for a range market

b > 1 pushes probabilities toward 0 and 1 (sharpens); a and c shift; d lets
range ("between") and threshold ("above") markets differ. Fitted by
penalised maximum likelihood, the penalty pulling toward the identity map
(a=0, b=1, c=d=0) so thin data changes little. Each contract counts once
(rows weighted 1 / rows-per-contract). Validated by k-fold over whole EVENTS
(one city-day), and a kind's map is only switched on when it beats the raw
probabilities out of fold.

Pure maths here; scripts/fit_nowcast_recalibration.py fits and writes the
parameters; the weather estimator applies them.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

VERSION = "recal@1"
EPS = 0.005


def logit(p):
    p = np.clip(np.asarray(p, dtype=float), EPS, 1 - EPS)
    return np.log(p / (1 - p))


def sigmoid(x):
    return 1.0 / (1.0 + np.exp(-np.asarray(x, dtype=float)))


def features(p, bracket):
    L = logit(p)
    B = np.asarray(bracket, dtype=float)
    return np.column_stack([np.ones_like(L), L, B, B * L])


IDENTITY = np.array([0.0, 1.0, 0.0, 0.0])


def fit_map(p, bracket, y, w=None, l2: float = 2.0, iters: int = 60) -> np.ndarray:
    """Penalised (toward IDENTITY) weighted logistic regression, Newton steps."""
    X = features(p, bracket)
    y = np.asarray(y, dtype=float)
    w = np.ones(len(y)) if w is None else np.asarray(w, dtype=float)
    beta = IDENTITY.copy()
    for _ in range(iters):
        q = sigmoid(X @ beta)
        g = X.T @ (w * (q - y)) + l2 * (beta - IDENTITY)
        H = (X * (w * q * (1 - q))[:, None]).T @ X + l2 * np.eye(4)
        step = np.linalg.solve(H, g)
        beta = beta - step
        if np.max(np.abs(step)) < 1e-8:
            break
    return beta


def apply_map(beta, p, bracket) -> np.ndarray:
    return sigmoid(features(p, bracket) @ np.asarray(beta, dtype=float))


def brier(p, y, w=None) -> float:
    p, y = np.asarray(p, dtype=float), np.asarray(y, dtype=float)
    w = np.ones(len(y)) if w is None else np.asarray(w, dtype=float)
    return float(np.sum(w * (p - y) ** 2) / np.sum(w))


def cross_validate(p, bracket, y, w, groups, k: int = 5, l2: float = 2.0, seed: int = 7) -> dict:
    """Out-of-fold Brier, raw vs recalibrated, folds over whole event groups."""
    groups = np.asarray(groups)
    uniq = np.unique(groups)
    rng = np.random.default_rng(seed)
    rng.shuffle(uniq)
    fold_of = {g: i % k for i, g in enumerate(uniq)}
    f = np.array([fold_of[g] for g in groups])
    p, bracket, y, w = map(np.asarray, (p, bracket, y, w))
    out = np.empty(len(y))
    for i in range(k):
        tr, te = f != i, f == i
        if not te.any() or tr.sum() < 20:
            out[te] = p[te]
            continue
        beta = fit_map(p[tr], bracket[tr], y[tr], w[tr], l2)
        out[te] = apply_map(beta, p[te], bracket[te])
    return {"brier_raw": brier(p, y, w), "brier_recal": brier(out, y, w),
            "events": int(len(uniq)), "folds": k}


class Recalibrator:
    """Read side: {kind: beta} for the kinds whose map passed validation."""

    def __init__(self, params: dict | None = None):
        self.params = params or {}
        self.maps = {k: np.array(v["beta"]) for k, v in (self.params.get("kinds") or {}).items()
                     if v.get("enabled")}

    @classmethod
    def load(cls, path: str | Path) -> "Recalibrator":
        try:
            return cls(json.loads(Path(path).read_text()))
        except (OSError, ValueError):
            return cls(None)

    @property
    def version(self) -> str:
        return f"{VERSION}:{str(self.params.get('fitted_at', ''))[:10]}"

    def applies(self, kind: str) -> bool:
        return kind in self.maps

    def __call__(self, kind: str, p: float, bracket: bool) -> float:
        beta = self.maps.get(kind)
        if beta is None or p is None or math.isnan(p):
            return p
        return float(apply_map(beta, [p], [1.0 if bracket else 0.0])[0])
