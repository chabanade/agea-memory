# CLAUDE.md - AGEA Memory System

## Project Overview

AGEA is a **personal AI memory system** for Mehdi (AGEA founder). It combines a Telegram bot, a FastAPI REST API, a Neo4j knowledge graph (Graphiti), and PostgreSQL for persistent conversation history. MCP servers bridge the memory to Claude and Cursor.

**Language**: The codebase mixes French (comments, logs, Telegram commands) and English (code identifiers, API).

## Architecture

```
Telegram <-> Bot (FastAPI + Telegram) <-> PostgreSQL (conversations + task queue)
                                      <-> Neo4j (Graphiti knowledge graph)
MCP Servers (Claude.ai / Cursor) <-> Bot REST API
Caddy (reverse proxy + auto-HTTPS) -> Bot + MCP
```

### Services (Docker Compose)

| Service | Image / Build | Port | Purpose |
|---------|--------------|------|---------|
| postgres | pgvector/pgvector:pg16 | 5432 (internal) | Conversations + Graphiti task queue |
| neo4j | neo4j:5.26.0 | 7687 (internal) | Knowledge graph (Graphiti + APOC) |
| bot | ../bot/Dockerfile (Python 3.12) | 8000 | Telegram bot + FastAPI API |
| mcp-remote | ../mcp-server/Dockerfile (Python 3.13) | 8888 | MCP HTTP server for Claude.ai |
| caddy | caddy:2-alpine | 80, 443 | Reverse proxy + auto HTTPS |

### Directory Structure

```
bot/              # Core: Telegram bot + FastAPI (Python)
  main.py         # Entry point (~1400 lines) - all endpoints + Telegram handlers
  llm_provider.py # Multi-provider LLM (DeepSeek > Qwen > Claude > Ollama)
  conversation_store.py  # PostgreSQL conversation storage
  graphiti_client.py     # Neo4j knowledge graph client
  graphiti_worker.py     # Async task queue worker
  intent_detector.py     # Regex-based intent classification
  voice_handler.py       # Groq Whisper transcription
  daily_summary.py       # Daily summary scheduler (Phase 6D)
  proactive.py           # Proactive reminders (Phase 6E)
  reasoning_models.py    # Pydantic models: Decision, Doubt, Lesson (Phase 7)
  reasoning_formatter.py # Format/validate reasoning structures
mcp-server/       # MCP bridge servers
  mcp-remote-server.py   # Streamable HTTP (port 8888, for Claude.ai)
  zep-bridge.py          # STDIO transport (for Claude Code/Cursor)
docker/           # Docker Compose orchestration
  docker-compose.yml
  caddy/Caddyfile
scripts/          # Deployment & operations
  deploy.sh       # VPS deploy via rsync + SSH
  backup.sh       # Daily PostgreSQL backup to S3
  init_conversations.sql
  init_graphiti_queue.sql
  migrate_zep_to_graphiti.py
n8n/              # n8n workflow definitions
sauvegardes-contexte/  # Timestamped git state snapshots (historical)
```

## Quick Commands

```bash
# Build and start all services
cd docker && docker compose --env-file ../.env up -d

# Build only the bot (after code changes)
cd docker && docker compose --env-file ../.env build --no-cache bot

# Restart a single service
docker compose restart bot

# View bot logs
docker logs docker-bot-1 --tail 100

# Deploy to VPS
./scripts/deploy.sh

# Health check
curl -s https://srv987452.hstgr.cloud/health
curl -s https://srv987452.hstgr.cloud/status
```

## Environment Setup

Copy `.env.example` to `.env` and fill in values. Key variables:

- **LLM_PROVIDER**: `deepseek` (default), `qwen`, `claude`, `ollama`
- **EMBEDDING_PROVIDER**: `gemini` (default, free), `ollama`
- **GRAPHITI_ENABLED** / **GRAPHITI_READ_ENABLED**: Feature flags for Neo4j knowledge graph
- **TELEGRAM_MODE**: `polling` (dev) or `webhook` (production with Caddy/HTTPS)
- **FEATURE_REASONING**: Enable Phase 7 structured reasoning (`true` by default)

## API Endpoints

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/health` | No | Health check (includes graphiti_available status) |
| GET | `/status` | No | Detailed system status |
| GET | `/api/context?q=...&limit=5` | Bearer | Semantic search in conversations |
| POST | `/api/memo` | Bearer | Save a memory (`{content, role}`) |
| GET | `/api/facts?q=...&limit=10` | Bearer | Search Graphiti knowledge graph |
| POST | `/api/correct` | Bearer | Correct existing facts (bi-temporal) |
| GET | `/api/entity/{name}` | Bearer | Get entity + relationships |
| POST | `/webhook/telegram` | Telegram token | Telegram webhook |

Auth token = `ZEP_SECRET_KEY` env var, passed as `Authorization: Bearer <token>`.

## MCP Tools (exposed to Claude/Cursor)

Both MCP servers expose: `search_memory`, `search_facts`, `save_memory`, `correct_fact`, `get_entity`, `get_history`, `search_decisions`.

## Key Technical Decisions

- **Zero-latency intent detection**: Pure regex, no LLM call for routing
- **Bi-temporal corrections**: Facts are marked invalid, never deleted
- **PostgreSQL task queue**: No Redis — uses `graphiti_tasks` table with `SKIP LOCKED`
- **Multi-provider LLM fallback chain**: DeepSeek -> Qwen -> Claude -> Ollama
- **Async architecture**: Full asyncio with FastAPI + asyncpg
- **Idempotent migrations**: Checkpoint-based migration system

## Development Conventions

- **Python 3.12+** for bot, **3.13** for MCP server
- Dependencies in `requirements.txt` (pinned major.minor with wildcard patch)
- No test framework currently — verify via healthchecks and manual API calls
- Logging: `logging` module with format `%(asctime)s [%(levelname)s] %(name)s: %(message)s`
- Feature flags via environment variables (`GRAPHITI_ENABLED`, `FEATURE_REASONING`, etc.)
- Pydantic models for request/response validation

## Deployment

- **VPS**: Hostinger (148.230.112.42), deployed to `/opt/agea`
- **Domain**: srv987452.hstgr.cloud
- **HTTPS**: Auto-managed by Caddy
- **Backups**: Daily PostgreSQL dump at 2 AM UTC+1, local retention 7 days, optional S3 (Scaleway)

## Troubleshooting

### Graphiti/Neo4j issues
```bash
# Check if Neo4j is up
cd /opt/agea/docker && docker compose ps

# Restart Neo4j and wait for startup
docker compose up -d neo4j && sleep 30

# Check bot logs for Graphiti errors
docker logs docker-bot-1 --tail 100 | grep -iE "graphiti|neo4j|error|failed"

# Restart bot after Neo4j is healthy
docker compose restart bot

# Verify health
curl -s https://srv987452.hstgr.cloud/health
# Expected: graphiti_available: true, graphiti_read_enabled: true
```

### Bot not responding
```bash
docker compose ps              # Check service statuses
docker logs docker-bot-1 -f    # Follow bot logs
docker compose restart bot     # Restart bot
```
