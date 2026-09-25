#!/usr/bin/env bash
# Runs the whole test suite. No network, no credentials, nothing at risk.
cd "$(dirname "$0")/.."
[ -d .venv ] && source .venv/bin/activate
clear
python tests/run_tests.py
echo
echo "Press Return to close."
read -r _
