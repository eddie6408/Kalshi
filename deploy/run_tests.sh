#!/bin/bash
# Gate for every deploy: must print "N passed, 0 failed".
cd "$(dirname "$0")/.." && .venv/bin/python tests/run_tests.py 2>&1 | tail -1
