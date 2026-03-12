#!/usr/bin/env bash
# run.sh — Thin wrapper to activate the venv and run the full pipeline.
# All arguments are forwarded to src/main.py.
#
# Examples:
#   ./run.sh                              # uses config.yaml defaults
#   ./run.sh --search "qa lead"           # override search term
#   ./run.sh --skip-scrape                # re-score + report from today's CSV

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

source venv/bin/activate
python src/main.py "$@"
