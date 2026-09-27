"""Run the whole suite: .venv/bin/python tests/run_tests.py  -> ends with 'N passed, M failed'."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if __name__ == "__main__":
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    res = unittest.TextTestRunner(verbosity=1).run(suite)
    failed = len(res.failures) + len(res.errors)
    print(f"{res.testsRun - failed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
