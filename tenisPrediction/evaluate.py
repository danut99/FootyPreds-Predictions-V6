"""Evaluare walk-forward: înveliș subțire peste ``tenisPrediction/benchmark.py``.

Implicit evaluează modelul v2 pe anii de validare 2023 și 2024, ATP și WTA::

    python -m tenisPrediction.evaluate
    python -m tenisPrediction.evaluate --baseline            # modelul compact v1
    python -m tenisPrediction.evaluate --tours atp,wta,challenger --json out.json

Orice alt argument trece neschimbat la benchmark (``--years``, ``--param``, ``--records``...).
Anul 2025 este testul blocat: benchmark-ul îl refuză fără ``--locked-test``.
"""

from __future__ import annotations

import sys

from . import benchmark

MODEL = "tenisPrediction.model:benchmark_factory"
BASELINE = "tenisPrediction.candidates.baseline:factory"


def build_argv(argv: list[str]) -> list[str]:
    args = list(argv)
    model = MODEL
    if "--baseline" in args:
        args.remove("--baseline")
        model = BASELINE

    def given(flag: str) -> bool:
        return any(arg == flag or arg.startswith(flag + "=") for arg in args)

    defaults = []
    if not given("--model"):
        defaults += ["--model", model]
    if not given("--years"):
        defaults += ["--years", "2023,2024"]
    if not given("--tours"):
        defaults += ["--tours", "atp,wta"]
    return defaults + args


def main(argv: list[str] | None = None) -> int:
    return benchmark.main(build_argv(sys.argv[1:] if argv is None else argv))


if __name__ == "__main__":
    sys.exit(main())
