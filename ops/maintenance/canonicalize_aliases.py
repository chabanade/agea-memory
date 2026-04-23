#!/usr/bin/env python3
"""
Canonicalize entity aliases in Neo4j via APOC mergeNodes.
Standalone maintenance script - no coupling to bot code.

Usage:
    python canonicalize_aliases.py [--dry-run] [--config PATH]

Env vars required:
    NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD
"""
import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from neo4j import GraphDatabase

logging.basicConfig(
    level=logging.INFO,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}',
    datefmt='%Y-%m-%dT%H:%M:%SZ',
)
log = logging.getLogger("canonicalize_aliases")

CYPHER_MERGE = """
MATCH (a:Entity {name: $alias, group_id: $gid})
MATCH (c:Entity {name: $canonical, group_id: $gid})
WHERE a.uuid <> c.uuid
WITH a, c
CALL apoc.refactor.mergeNodes([c, a], {
    properties: 'discard',
    mergeRels: true
}) YIELD node
RETURN node.uuid AS merged_uuid, node.name AS canonical_name
"""

CYPHER_DRY_RUN = """
MATCH (a:Entity {name: $alias, group_id: $gid})
MATCH (c:Entity {name: $canonical, group_id: $gid})
WHERE a.uuid <> c.uuid
RETURN a.uuid AS alias_uuid, a.name AS alias_name, count{(a)--()} AS alias_edges,
       c.uuid AS canonical_uuid, c.name AS canonical_name, count{(c)--()} AS canonical_edges
"""


def load_config(config_path: Path) -> dict:
    if not config_path.exists():
        log.error(f"Config absent: {config_path}")
        sys.exit(2)
    with config_path.open() as f:
        cfg = json.load(f)
    if "aliases" not in cfg or "group_id" not in cfg:
        log.error("Config invalide: 'aliases' et 'group_id' requis")
        sys.exit(2)
    return cfg


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="N'execute pas le merge, log seulement")
    parser.add_argument("--config", type=Path, default=Path(__file__).parent / "entity_aliases.json")
    args = parser.parse_args()

    cfg = load_config(args.config)
    gid = cfg["group_id"]
    aliases = cfg["aliases"]

    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user = os.environ.get("NEO4J_USER", "neo4j")
    pwd = os.environ.get("NEO4J_PASSWORD")
    if not pwd:
        log.error("NEO4J_PASSWORD non defini")
        sys.exit(3)

    log.info(f"start mode={'DRY-RUN' if args.dry_run else 'APPLY'} aliases_count={len(aliases)} gid={gid}")

    driver = GraphDatabase.driver(uri, auth=(user, pwd))
    merged_total = 0
    skipped = 0
    errors = 0

    try:
        with driver.session(database="neo4j") as session:
            for alias, canonical in aliases.items():
                try:
                    if args.dry_run:
                        result = session.run(CYPHER_DRY_RUN, alias=alias, canonical=canonical, gid=gid)
                        records = list(result)
                        if not records:
                            log.info(f"dry-run alias={alias} canonical={canonical} action=NOOP (0 noeud a merger)")
                            skipped += 1
                        else:
                            for rec in records:
                                log.info(
                                    f"dry-run alias={alias} action=WOULD_MERGE "
                                    f"alias_uuid={rec['alias_uuid']} alias_edges={rec['alias_edges']} "
                                    f"canonical_uuid={rec['canonical_uuid']} canonical_edges={rec['canonical_edges']} "
                                    f"final_edges_estimate={rec['alias_edges'] + rec['canonical_edges']}"
                                )
                    else:
                        result = session.run(CYPHER_MERGE, alias=alias, canonical=canonical, gid=gid)
                        records = list(result)
                        if not records:
                            log.info(f"alias={alias} action=NOOP (idempotent, deja fusionne ou inexistant)")
                            skipped += 1
                        else:
                            for rec in records:
                                log.info(f"alias={alias} action=MERGED canonical_uuid={rec['merged_uuid']}")
                                merged_total += 1
                except Exception as e:
                    log.error(f"alias={alias} canonical={canonical} ERROR: {e}")
                    errors += 1
    finally:
        driver.close()

    log.info(f"done merged={merged_total} skipped={skipped} errors={errors}")
    sys.exit(0 if errors == 0 else 1)


if __name__ == "__main__":
    main()
