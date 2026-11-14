#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
command -v python3 >/dev/null || { echo '请先安装 python3 python3-venv'; exit 1; }
if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv || { echo 'Ubuntu 24: sudo apt-get update && sudo apt-get install -y python3-venv'; exit 1; }
fi
.venv/bin/python -m pip -q install -r requirements.txt
exec .venv/bin/python scripts/probe.py "$@"
