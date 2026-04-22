#!/bin/bash
# Wrapper cron : source .env + run script Python
set -e
SCRIPT_DIR="/opt/agea/ops/maintenance"
ENV_FILE="/opt/agea/docker/.env"

set -a
source "$ENV_FILE"
set +a

export NEO4J_URI="bolt://localhost:7687"

if [ -f /opt/agea/.venv/bin/python ]; then
    exec /opt/agea/.venv/bin/python "$SCRIPT_DIR/canonicalize_aliases.py" "$@"
else
    exec python3 "$SCRIPT_DIR/canonicalize_aliases.py" "$@"
fi
