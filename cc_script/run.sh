#!/usr/bin/env bash
set -euo pipefail
source cc_script/config.sh
exec python3 cc_script/lib/submit.py "$@"
