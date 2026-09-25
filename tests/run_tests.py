#!/usr/bin/env python3
"""Zero-dependency test runner.

If you have pytest installed, just use:  python -m pytest tests -q
This runner exists so the suite works on a machine with nothing but Python.

    python tests/run_tests.py

THIS is the runner run_bot.sh uses at startup, and it refuses to start the bot
on a failure. So a test that only passes under pytest stops the bot dead.

The shim below provides `pytest.raises` and `pytest.approx`. It does NOT
provide fixtures or marks. In particular:

    caplog                  -> attach a logging.Handler yourself
    pytest.mark.parametrize -> write a loop inside one test
    tmp_path / monkeypatch  -> use tempfile / try..finally

Both of those have already cost a startup once. Write the plain version.
"""
from __future__ import annotations

import importlib.util
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


class _Raises:
    def __init__(self, exc):
        self.exc = exc
        self.value = None

    def __enter__(self):
        return self

    def __exit__(self, t, v, tb):
        if t is None:
            raise AssertionError(f"expected {self.exc.__name__} but nothing was raised")
        if not issubclass(t, self.exc):
            return False
        self.value = v
        return True


class _PytestShim:
    raises = staticmethod(lambda exc: _Raises(exc))

    @staticmethod
    def approx(value, abs=1e-9, rel=None):  # noqa: A002
        class _A:
            def __eq__(self, other):
                return abs_(other - value) <= abs
        abs_ = __builtins__["abs"] if isinstance(__builtins__, dict) else __builtins__.abs
        return _A()


sys.modules.setdefault("pytest", _PytestShim())  # type: ignore[arg-type]


def main() -> int:
    files = sorted(p for p in HERE.glob("test_*.py"))
    passed = failed = 0
    failures = []

    for path in files:
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(module)   # type: ignore[union-attr]
        except Exception:
            failures.append((path.name, "<import>", traceback.format_exc()))
            failed += 1
            continue

        print(f"\n{path.name}")
        for name in sorted(dir(module)):
            if not name.startswith("test_"):
                continue
            fn = getattr(module, name)
            if not callable(fn):
                continue
            try:
                fn()
                passed += 1
                print(f"  .  {name}")
            except Exception:
                failed += 1
                failures.append((path.name, name, traceback.format_exc()))
                print(f"  F  {name}")

    print("\n" + "=" * 70)
    for fname, tname, tb in failures:
        print(f"\nFAILED {fname}::{tname}\n{tb}")
    print("=" * 70)
    print(f"{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
