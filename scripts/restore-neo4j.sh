#!/bin/bash
# ===========================================
# Restauration Neo4j depuis un backup tar.gz
# ===========================================
# Usage : ./restore-neo4j.sh [fichier.tar.gz]
#
# Si aucun fichier specifie, utilise le backup le plus recent.
# ATTENTION : Arrete Neo4j, ecrase les donnees, redemarre.
# ===========================================

set -euo pipefail

BACKUP_DIR="/opt/agea/backups"
COMPOSE_DIR="/opt/agea/docker"

# Determiner le fichier a restaurer
if [ $# -ge 1 ]; then
    BACKUP_FILE="$1"
else
    BACKUP_FILE=$(ls -t "${BACKUP_DIR}"/agea-neo4j-*.tar.gz 2>/dev/null | head -1)
    if [ -z "$BACKUP_FILE" ]; then
        echo "ERREUR: Aucun backup Neo4j trouve dans ${BACKUP_DIR}"
        exit 1
    fi
fi

if [ ! -f "$BACKUP_FILE" ]; then
    echo "ERREUR: Fichier non trouve: $BACKUP_FILE"
    exit 1
fi

SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
echo "=== Restauration Neo4j ==="
echo "Fichier : $BACKUP_FILE ($SIZE)"
echo ""
echo "ATTENTION : Cette operation va :"
echo "  1. Arreter le bot, mcp-remote et Neo4j"
echo "  2. Ecraser le volume neo4j_data"
echo "  3. Redemarrer tous les services"
echo ""
read -p "Continuer ? (oui/non) : " confirm
if [ "$confirm" != "oui" ]; then
    echo "Annule."
    exit 0
fi

cd "$COMPOSE_DIR"

echo "[1/4] Arret des services..."
docker compose --env-file ../.env stop bot mcp-remote neo4j

echo "[2/4] Restauration du volume data..."
docker run --rm \
    -v docker_neo4j_data:/data \
    -v "$(dirname "$BACKUP_FILE"):/backup:ro" \
    alpine \
    sh -c "rm -rf /data/* && tar xzf /backup/$(basename "$BACKUP_FILE") -C /data"

echo "[3/4] Redemarrage des services..."
docker compose --env-file ../.env up -d

echo "[4/4] Verification..."
sleep 15
docker compose --env-file ../.env ps

echo ""
echo "=== Restauration terminee ==="
echo "Verifier les logs : docker compose --env-file ../.env logs neo4j --tail 20"
