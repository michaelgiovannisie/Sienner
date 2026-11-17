#!/usr/bin/env bash
# Set up a working environment and run the FMP field-semantics spike.
#
# Exists because the spike can only run on a machine that can reach
# financialmodelingprep.com, and because macOS ships Python 3.9 while Sienner
# needs 3.10+ (slots=True dataclasses, PEP 604 unions in runtime-evaluated
# positions).
#
#   ./scripts/run_spike.sh
#
# Idempotent: re-running reuses the venv if it is already on a usable Python.

set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"
VENV="$ROOT/.venv"

MIN_MINOR=10

usable() {
    # usable <python> -> 0 if it is CPython 3.MIN_MINOR or newer
    "$1" -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, $MIN_MINOR) else 1)" \
        >/dev/null 2>&1
}

find_python() {
    # Prefer the newest explicitly-versioned interpreter on PATH, then the
    # Homebrew locations, then a bare python3.
    local candidate
    for candidate in python3.13 python3.12 python3.11 python3.10 \
                     /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 \
                     /opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3.10 \
                     /usr/local/bin/python3.12 /usr/local/bin/python3.11 \
                     python3; do
        if command -v "$candidate" >/dev/null 2>&1 && usable "$candidate"; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

# --- 1. interpreter ---------------------------------------------------------

if [ -x "$VENV/bin/python" ] && usable "$VENV/bin/python"; then
    echo "==> existing .venv is on a usable Python"
else
    if [ -d "$VENV" ]; then
        echo "==> .venv is on Python 3.9 or older; rebuilding"
        rm -rf "$VENV"
    fi

    if ! PYTHON="$(find_python)"; then
        cat >&2 <<'MSG'
ERROR: no Python 3.10 or newer found.

macOS ships 3.9, which Sienner does not support. Install a newer one:

    brew install python@3.12

then run this script again.
MSG
        exit 1
    fi

    echo "==> building .venv with $PYTHON ($("$PYTHON" -V 2>&1))"
    "$PYTHON" -m venv "$VENV"
fi

# --- 2. dependencies --------------------------------------------------------

# pip older than 21.3 cannot do an editable install from a bare pyproject.toml
# (PEP 660), which is the error macOS's bundled pip 21.2.4 produces.
echo "==> upgrading pip"
"$VENV/bin/python" -m pip install --quiet --upgrade pip

echo "==> installing backend (editable, with dev extras)"
"$VENV/bin/python" -m pip install --quiet -e "$ROOT/backend[dev]"

# --- 3. key check -----------------------------------------------------------

if ! "$VENV/bin/python" - <<'PY'
import sys
sys.path.insert(0, "backend")
from sienner.config import Settings
sys.exit(0 if Settings().fmp_api_key else 1)
PY
then
    echo "ERROR: SIENNER_FMP_API_KEY is empty. Set it in $ROOT/.env" >&2
    exit 1
fi
echo "==> API key found"

# --- 4. suite, then the spike ----------------------------------------------

echo "==> running the test suite"
(cd "$ROOT/backend" && "$VENV/bin/python" -m pytest -q)

echo
echo "==> running the spike"
cd "$ROOT/backend"
exec "$VENV/bin/python" -m sienner.scripts.fmp_spike \
    --ticker NVDA --ticker AAPL --ticker KO "$@"
