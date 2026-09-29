"""Calibrare în formă de S a probabilității afișate (tenisPrediction v3).

Every calibrator here is an antisymmetric map ``g`` of the winner-first logit ``z``
(``g(-z) = -g(z)``, so the calibrated model stays exactly symmetric) fitted on a rolling window
of out-of-sample predictions whose results are known. The window holds winner-first logits
(``+|z|`` when the favourite won, ``-|z|`` otherwise), so the fit is a logistic regression
with ``y = 1`` and no intercept:

- ``t``:   temperature ``g(z) = a z``;
- ``s2``:  S-curve ``g(z) = a z + b z |z|`` (a convex/concave bend on each side, which is what
  the over-confident tail of a season needs);
- ``iso``: symmetrised isotonic regression on ``|z|`` (PAV over quantile bins, knots on the
  logit scale, linear between knots).

``fit(mode, z, half_life)`` returns the parameters, ``apply(mode, params, z)`` the calibrated
logit. Non-monotone fits (``b < 0`` past the peak) are flattened, never reversed.
"""

from __future__ import annotations

import math

import numpy as np

MODES = ("none", "t", "s2", "iso")
_Z_MAX = 8.0


def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 0.5 * (1.0 + np.tanh(0.5 * x))


def _design(u: np.ndarray, mode: str) -> np.ndarray:
    if mode == "t":
        return u[:, None]
    if mode == "s2":
        return np.stack([u, u * np.abs(u)], axis=1)
    raise ValueError(f"Mod de calibrare necunoscut: {mode!r}; permise: {MODES}")


def _weights(n: int, half_life: float | None) -> np.ndarray:
    if not half_life:
        return np.ones(n)
    age = (n - 1 - np.arange(n)).astype(float)
    return 0.5 ** (age / half_life)


def fit_glm(z: np.ndarray, mode: str, half_life: float | None = None, l2: float = 1.0):
    """(a[, b]) of ``g(z) = a z (+ b z|z|)`` by damped Newton on winner-first logits.

    The ridge pulls towards the identity (a = 1, b = 0), so a short window cannot bend the
    curve wildly.
    """
    z = np.asarray(z, dtype=float)
    x = _design(z, mode)  # rows are signed already: x @ beta is the winner-first logit
    w = _weights(len(z), half_life)
    k = x.shape[1]
    prior = np.zeros(k)
    prior[0] = 1.0
    beta = prior.copy()

    def objective(b):
        return float(w @ np.logaddexp(0.0, -(x @ b))) + 0.5 * l2 * float((b - prior) @ (b - prior))

    value = objective(beta)
    for _ in range(50):
        s = _sigmoid(x @ beta)
        grad = -(x.T @ (w * (1.0 - s))) + l2 * (beta - prior)
        hess = (x * (w * s * (1.0 - s))[:, None]).T @ x + l2 * np.eye(k)
        step = np.linalg.solve(hess, grad)
        size = 1.0
        while size > 1e-4:
            trial = beta - size * step
            trial_value = objective(trial)
            if trial_value <= value + 1e-12:
                break
            size *= 0.5
        else:
            break
        beta, done = trial, value - trial_value < 1e-10
        value = trial_value
        if done:
            break
    return tuple(float(v) for v in beta)


def apply_glm(params, mode: str, z: np.ndarray) -> np.ndarray:
    z = np.asarray(z, dtype=float)
    u = np.abs(z)
    a = params[0]
    if mode == "t":
        return max(a, 0.0) * z
    b = params[1]
    if b < 0.0 and a > 0.0:
        u = np.minimum(u, -a / (2.0 * b))  # flatten past the peak instead of turning back
    return np.sign(z) * np.maximum(0.0, a * u + b * u * u)


def fit_isotonic(z: np.ndarray, bins: int = 8):
    """Knots (|z| means, logit of the pooled accuracy) of a monotone step fit on |z|."""
    z = np.asarray(z, dtype=float)
    u = np.abs(z)
    hit = (z > 0).astype(float) + 0.5 * (z == 0)
    order = np.argsort(u, kind="stable")
    u, hit = u[order], hit[order]
    edges = np.linspace(0, len(u), bins + 1).astype(int)
    blocks = [
        [hit[a:b].mean(), b - a, u[a:b].sum()] for a, b in zip(edges[:-1], edges[1:]) if b > a
    ]
    i = 0
    while i < len(blocks) - 1:  # pool adjacent violators
        if blocks[i][0] > blocks[i + 1][0]:
            n = blocks[i][1] + blocks[i + 1][1]
            y = (blocks[i][0] * blocks[i][1] + blocks[i + 1][0] * blocks[i + 1][1]) / n
            blocks[i] = [y, n, blocks[i][2] + blocks[i + 1][2]]
            del blocks[i + 1]
            i = max(i - 1, 0)
        else:
            i += 1
    kx, ky = [0.0], [0.0]
    for y, n, total in blocks:
        kx.append(total / n)
        y = min(max(y, 0.5 + 1e-4), 1.0 - 1e-4)
        ky.append(math.log(y / (1.0 - y)))
    return tuple(kx), tuple(np.maximum.accumulate(ky).tolist())


def apply_isotonic(knots, z: np.ndarray) -> np.ndarray:
    kx, ky = (np.asarray(k, dtype=float) for k in knots)
    z = np.asarray(z, dtype=float)
    u = np.abs(z)
    g = np.interp(u, kx, ky)
    far = u > kx[-1]
    g[far] = ky[-1] + (u[far] - kx[-1])  # identity slope beyond the last knot
    return np.sign(z) * g


def fit(mode: str, z, half_life: float | None = None):
    if mode == "iso":
        return fit_isotonic(z)
    return fit_glm(z, mode, half_life)


def apply(mode: str, params, z):
    """Calibrated winner-first logit(s); ``mode="none"`` or ``params=None`` is the identity."""
    if mode == "none" or params is None:
        return z
    scalar = np.isscalar(z)
    values = np.atleast_1d(np.asarray(z, dtype=float))
    values = np.clip(values, -_Z_MAX, _Z_MAX)
    out = apply_isotonic(params, values) if mode == "iso" else apply_glm(params, mode, values)
    return float(out[0]) if scalar else out
