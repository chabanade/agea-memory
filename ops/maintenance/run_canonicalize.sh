#!/bin/bash
# Wrapper cron : execute le script dans le container bot
# (reutilise neo4j driver deja installe + reseau docker natif)
set -e
SCRIPT_DIR="/opt/agea/ops/maintenance"
ENV_FILE="/opt/agea/docker/.env"
BOT_CONTAINER="docker-bot-1"

set -a
source "$ENV_FILE"
set +a

docker cp "$SCRIPT_DIR/canonicalize_aliases.py" "${BOT_CONTAINER}:/tmp/canonicalize_aliases.py"
docker cp "$SCRIPT_DIR/entity_aliases.json"    "${BOT_CONTAINER}:/tmp/entity_aliases.json"

exec docker exec \
    -e NEO4J_URI="bolt://neo4j:7687" \
    -e NEO4J_USER="$NEO4J_USER" \
    -e NEO4J_PASSWORD="$NEO4J_PASSWORD" \
    "$BOT_CONTAINER" \
    python /tmp/canonicalize_aliases.py --config /tmp/entity_aliases.json "$@"
