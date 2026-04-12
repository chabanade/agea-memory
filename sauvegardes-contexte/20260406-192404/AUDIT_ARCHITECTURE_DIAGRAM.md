# ARCHITECTURE DIAGRAM - AGEA SYSTEM
## Vue d'ensemble & Flux de Données

---

## SYSTÈME ACTUEL (Single Instance)

```
┌─────────────────────────────────────────────────────────────────┐
│                      VPS HOSTINGER (148.230.112.42)             │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │               Docker Compose (docker/)                   │  │
│  │                                                          │  │
│  │  ┌──────────────────────────────────────────────────┐   │  │
│  │  │  CADDY (Reverse Proxy + HTTPS Auto)               │   │  │
│  │  │  ├─ Ports: 80, 443                               │   │  │
│  │  │  ├─ Let's Encrypt: ✅ (auto-renew)               │   │  │
│  │  │  ├─ Routes: /api/* /webhook/* /mcp* /health      │   │  │
│  │  │  └─ ❌ Rate limiting: ABSENT                      │   │  │
│  │  │  └─ ❌ Timeout upstream: DEFAULT INFINI           │   │  │
│  │  └──────────────────────────────────────────────────┘   │  │
│  │              │                    │         │             │  │
│  │              ▼                    ▼         ▼             │  │
│  │  ┌──────────────────┐  ┌────────────────┐  ┌──────────┐  │  │
│  │  │   BOT (FastAPI)  │  │  MCP-REMOTE    │  │ (Future) │  │  │
│  │  │   Port: 8000     │  │  Port: 8888    │  │  n8n     │  │  │
│  │  │                  │  │                │  └──────────┘  │  │
│  │  │ ✅ Healthcheck   │  │ ✅ Healthcheck │                │  │
│  │  │ ✅ Async workers │  │ - Bridge HTTP  │                │  │
│  │  │ ❌ No timeout    │  │ ❌ Timeout 30s │                │  │
│  │  │ ❌ No circuit br.│  │ ❌ No retry    │                │  │
│  │  └────────┬─────────┘  └────────────────┘                │  │
│  │           │                                               │  │
│  │   ┌───────┴───────────────────────────────────────┐      │  │
│  │   │                                               │      │  │
│  │   ▼                                               ▼      │  │
│  │  ┌────────────────┐              ┌──────────────────┐  │  │
│  │  │  PostgreSQL    │              │    Neo4j         │  │  │
│  │  │  pgvector:pg16 │              │    5.26.0        │  │  │
│  │  │                │              │                  │  │  │
│  │  │ Conversations  │              │ Knowledge Graph  │  │  │
│  │  │ Queue Graphiti │              │ Entities/Facts   │  │  │
│  │  │ Sessions       │              │ Bi-temporal      │  │  │
│  │  │                │              │                  │  │  │
│  │  │ ✅ Healthcheck │              │ ✅ Healthcheck   │  │  │
│  │  │ ❌ No replica  │              │ ❌ No backup     │  │  │
│  │  │ ❌ No WAL      │              │ ❌ No replication│  │  │
│  │  │                │              │                  │  │  │
│  │  │ Volume:        │              │ Volumes:         │  │  │
│  │  │ postgres_data  │              │ neo4j_data       │  │  │
│  │  │ (LOCAL ONLY)   │              │ neo4j_logs       │  │  │
│  │  │ = SPOF ⚠️     │              │ (LOCAL ONLY)     │  │  │
│  │  │                │              │ = SPOF ⚠️        │  │  │
│  │  └────────────────┘              └──────────────────┘  │  │
│  │                                                          │  │
│  │  ┌────────────────────────────────────────────────────┐ │  │
│  │  │  GraphitiWorker (Async Background Task)            │ │  │
│  │  │  ├─ Consomme queue PostgreSQL                      │ │  │
│  │  │  ├─ Ingest episodes Neo4j (via graphiti-core)      │ │  │
│  │  │  ├─ Embedding: Gemini API (1000/jour quota)        │ │  │
│  │  │  ├─ Fallback: Ollama local (optional)              │ │  │
│  │  │  ├─ Backoff exponentiel (max 5 retry)              │ │  │
│  │  │  ├─ Daily quota limit: 600 tasks                   │ │  │
│  │  │  └─ ❌ No circuit breaker                          │ │  │
│  │  │  └─ ❌ Quota counter reset on restart              │ │  │
│  │  └────────────────────────────────────────────────────┘ │  │
│  │                                                          │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │  Backup System (Cron Scripts)                            │  │
│  │  ├─ PostgreSQL: Daily dump (gzip) + S3 upload           │  │
│  │  ├─ Neo4j: ❌ MISSING                                    │  │
│  │  ├─ Rétention: 7 jours local, ∞ S3                      │  │
│  │  └─ Restore test: ❌ NEVER TESTED                       │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘

External APIs (Internet):
  ┌─────────┐    ┌─────────┐    ┌──────────┐    ┌──────────┐
  │ DeepSeek│    │  Qwen   │    │  Claude  │    │  Gemini  │
  │  (LLM)  │    │  (LLM)  │    │  (LLM)   │    │ (Embed)  │
  │ $0.28   │    │ $0.26   │    │ Fallback │    │$0 (free) │
  │per1Ktok │    │per1Ktok │    │ Premium  │    │1000/day  │
  └─────────┘    └─────────┘    └──────────┘    └──────────┘
       ▲              ▲              ▲                ▲
       │              │              │                │
       └──────────────┴──────────────┴────────────────┘
                      │
                      │ (Fallback chain)
              ┌───────┴────────┐
              │   Bot FastAPI   │
              │   LLMProvider   │
              └─────────────────┘

Messaging:
  ┌──────────────┐      ┌───────────────┐
  │  Telegram    │◄────►│  Bot API      │
  │  Users       │      │  :8000/webhook│
  └──────────────┘      └───────────────┘

Claude.ai Integration:
  ┌───────────────────┐      ┌────────────────┐
  │  Claude Code /    │◄────►│  MCP Remote    │
  │  Cursor IDE       │      │  Server :8888  │
  │  (via MCP tools)  │      │  (HTTP bridge) │
  └───────────────────┘      └────────────────┘
```

---

## REQUEST FLOW - MESSAGE TELEGRAM

```
User (Mehdi) writes message on Telegram
        │
        ▼ (Polling or Webhook)
    Telegram API
        │
        ▼ (Reverse Proxy via Caddy)
    Caddy :443 /webhook/telegram
        │
        ├─ ❌ No rate limit → check
        ├─ ❌ No timeout → default httpd timeout
        ▼
    Bot FastAPI :8000
        │
        ├─ POST /webhook/telegram
        │   ├─ Parse telegram request
        │   ├─ Check TELEGRAM_ALLOWED_USERS
        │   ├─ store in ConversationStore (PostgreSQL)
        │
        ├─ Detect intent / business tag
        │   └─ LLM call (may fallback) → 30s timeout ✅
        │
        ├─ Query Graphiti context
        │   ├─ search_memory() → PostgreSQL
        │   ├─ search_facts() → Neo4j
        │   └─ get_entity() → Neo4j
        │       ❌ No timeout specified
        │
        ├─ Generate response via LLM
        │   ├─ LLMProvider.chat()
        │   ├─ Try DeepSeek (30s timeout)
        │   ├─ If fail → Try Qwen (15s timeout)
        │   ├─ If fail → Try Claude (30s timeout)
        │   ├─ If all fail → error
        │   │   ❌ No circuit breaker → retry chain every time
        │   └─ ✅ 1 retry per provider on 429/502/503
        │
        ├─ Enqueue Graphiti task
        │   ├─ INSERT INTO graphiti_tasks
        │   ├─ GraphitiWorker processes async
        │   │   ├─ Extract entities/facts
        │   │   ├─ Embed text → Gemini API
        │   │   │   ❌ No cache → API call every time
        │   │   │   ❌ Quota 1000/day → pause if exceeded
        │   │   ├─ Ingest to Neo4j
        │   │   └─ Mark task complete
        │   └─ ✅ Backoff exponentiel, max 5 retry
        │
        ├─ Send response via Telegram
        │   └─ Telegram API call
        │
        └─ Return 200 OK to Caddy

Timeline (best case):
  100ms   : Caddy reverse proxy
  200ms   : Bot request parsing
  2000ms  : LLM response (DeepSeek fast)
  100ms   : Graphiti query
  100ms   : Response formatting
  500ms   : Telegram API send
  ─────────
  3000ms  : Total (3 seconds)

Timeline (worst case):
  100ms   : Caddy
  200ms   : Parse
  30000ms : DeepSeek timeout
  15000ms : Qwen timeout
  30000ms : Claude timeout (FAIL)
  200ms   : Error response
  ─────────
  75500ms : 75 SECONDS ❌ (User sees "bot typing" for 75s!)

❌ NO CIRCUIT BREAKER = This loop repeats every message!
```

---

## GRAPHITI WORKER - ASYNC QUEUE PROCESSING

```
PostgreSQL:
┌──────────────────────────────────────┐
│ graphiti_tasks table                 │
│                                      │
│ id | user_id | episode | status | .. │
│ 1  | mehdi   | {...}   | pending|    │
│ 2  | mehdi   | {...}   | pending|    │
│ 3  | mehdi   | {...}   | pending|    │
│ ... 100+ rows when Mehdi sends 100 msgs
└──────────────────────────────────────┘
        │
        ▼ (GraphitiWorker polling every 5-10s)

    Worker Loop:
    ├─ Fetch next pending task
    ├─ Quota check: ❌ No persistence on restart
    │   └─ If > 600 today: pause 5min
    ├─ Extract embedding via Gemini
    │   ├─ ❌ No cache (API call every time same text)
    │   ├─ ✅ Retry on 429
    │   └─ ❌ Quota 1000/day = bottleneck at 20 concurrent users
    ├─ Ingest to Neo4j
    │   ├─ Create/Update nodes
    │   ├─ Establish relationships
    │   └─ Bi-temporal updates
    ├─ Mark task complete
    └─ ❌ No circuit breaker (loops forever if Neo4j down)

❌ SPOF: If Neo4j crashes:
   - Worker hangs on ingest
   - Tasks accumulate in queue
   - No alerting
   - Mehdi discovers when Neo4j finally comes back
   - 1000+ tasks try to process at once
   - Gemini quota exhausted in 1 hour
```

---

## FAILURE SCENARIOS

### Scenario 1: PostgreSQL Disk Full
```
Timeline:
  T+0 : PostgreSQL OOM on writes
  T+0 : ConversationStore INSERT fails
  T+0 : Bot returns 500 error
  T+0 : Caddy logs 500
  T+?: Mehdi notices bot doesn't respond (manual check)
  T+2h: SRE finds disk full, expands volume
  T+2h: PostgreSQL recovery
  T+2h: Service resumes

RTO : 2+ hours (unplanned downtime)
RPO : Conversations during outage = LOST
```

### Scenario 2: Neo4j Crashes
```
Timeline:
  T+0 : Neo4j OOM or corrupted index
  T+0 : Neo4j stops, healthcheck fails
  T+0 : Bot still starts (depends_on service_healthy passed before)
  T+1m: Bot attempts Graphiti queries → timeout
  T+1m: GraphitiWorker hangs on ingest
  T+?: Mehdi sends messages → "bot typing" for 60s+ (timeout)
  T+?: Messages get slow responses (2-3 min latency)
  T+10m: Neo4j comes back online
  T+10m: Worker resumes, 1000+ tasks queue
  T+1h: Gemini quota exhausted
  T+1h: Worker pauses 5 min, nothing happens
  T+24h: Quota reset, worker resumes

RTO : 10 minutes (slow degradation)
RPO : Knowledge graph during outage = LOST FOREVER (irreplaceable)
```

### Scenario 3: Gemini API Quota Exhausted
```
Timeline:
  T+0 : User surge (10 messages in 1 minute)
  T+5m: GraphitiWorker processes 10 tasks
  T+5m: 10 embedding API calls to Gemini
  T+7m: Daily quota = 600/1000 used
  T+3h: 600/600 quota exhausted (429 responses)
  T+3h: Worker pauses 5 min
  T+3h: Mehdi can't add new knowledge
  T+24h: Quota reset

RTO : 5 minutes pause (acceptable)
RPO : New knowledge skipped, retry next day
❌ Issue: If Mehdi mass-imports 100 docs → quota exhausted in 10 min
```

### Scenario 4: Caddy Certificate Renewal Fails
```
Timeline:
  T+0 : Let's Encrypt cert expires
  T+0 : Caddy auto-renewal triggered
  T+0 : DNS lookup fails or API rate limit
  T+1m: Cert remains expired
  T+?: Users get SSL error on https://srv987452.hstgr.cloud
  T+60min: Renewal retry (Caddy checks every hour)
  T+? : Manual intervention needed

RTO : 1-2 hours (depends on renewal retry interval)
```

### Scenario 5: VPS Hostinger Down
```
Timeline:
  T+0 : Hosting provider network outage
  T+0 : All services down (DB, Bot, Caddy)
  T+?: Mehdi notices no response
  T+1h: Hostinger resolves
  T+1h: Services auto-restart (restart: unless-stopped)
  T+2min: Bot healthy
  T+?: Service resumes

RTO : ~2 hours + 2 min restart
RPO : PostgreSQL: Depends on backup (S3)
      Neo4j: ZERO (no backup exists)

❌ WORST CASE: Neo4j lost forever
```

---

## MONITORING GAPS

```
What we CAN see:
  ✅ docker compose ps
     Shows if containers running

  ✅ docker logs bot
     Shows stdout/stderr (last 100 lines)

  ✅ curl http://localhost:8000/health
     Returns 200 if bot responsive

What we CANNOT see:
  ❌ LLM response times (DeepSeek vs Claude fallback)
  ❌ Graphiti queue backlog size
  ❌ Gemini quota % consumed today
  ❌ Database CPU/Memory/Disk trend
  ❌ API endpoint latency (p50, p95, p99)
  ❌ Error rate per endpoint
  ❌ Failed retry count
  ❌ Timeout count per provider
  ❌ Circuit breaker state

Result:
  ❌ Mehdi discovers problems from user complaints
  ❌ No proactive alerting
  ❌ No SLA visibility
  ❌ No capacity planning data
```

---

## SCALABILITY LIMITS

```
Current Max Concurrent Users: 3-5 (before bottleneck)

Bottleneck Analysis:
┌────────────────────────┬─────────┬──────────────┬─────────┐
│ Component              │ Limit   │ Current Util │ Max Util│
├────────────────────────┼─────────┼──────────────┼─────────┤
│ asyncpg pool (max=3)   │ 3 conn  │ 50% at 3 usr │ 100%    │
│ Gemini quota (1000/d)  │ 1000    │ ~50 at 3 usr │ 600/day │
│ Neo4j heap (512m)      │ 512M    │ 40% at 3 usr │ 80%     │
│ Bot memory (no limit)  │ ∞       │ 400m at 3 usr│ OOM →  │
│ Caddy connections      │ ∞       │ 10 at 3 usr  │ 1000s  │
│ LLM retry chain        │ 90s max │ 2s at 3 usr  │ Timeout │
└────────────────────────┴─────────┴──────────────┴─────────┘

Scaling to 10 concurrent users requires:
  ✅ asyncpg pool: min=5, max=20
  ✅ Gemini fallback: Ollama local
  ✅ Neo4j memory: 2G
  ✅ Bot memory: 1.5G limit
  ✅ Circuit breaker (LLM)
  ✅ Timeout global (30s)

Scaling to 50 concurrent users requires:
  ✅ PostgreSQL replication (Primary + Standby)
  ✅ Neo4j cluster (3 nodes)
  ✅ Load balancer (Caddy x2 or HAProxy)
  ✅ Message queue (RabbitMQ/Redis for async)
  ✅ Redis for caching

Scaling to 100+ users requires:
  ✅ Kubernetes (auto-scaling)
  ✅ Database sharding
  ✅ Service mesh (Istio)
  ✅ Observability stack (Prometheus, Loki, Jaeger)
```

---

## RECOMMENDED ARCHITECTURE (6 MONTHS)

```
┌───────────────────────────────────────────────────────────────┐
│         Multi-Region HA Setup (Europe + Backup)               │
│                                                               │
│  ┌────────────────────────┐  ┌────────────────────────┐      │
│  │  PRIMARY REGION (EU)   │  │  STANDBY REGION (US)   │      │
│  │  (Scaleway/AWS)        │  │  (Async Replica)       │      │
│  │                        │  │                        │      │
│  │ ┌──────────────────┐   │  │ ┌──────────────────┐   │      │
│  │ │ Kubernetes (3x)  │   │  │ │ PostgreSQL Standby
│  │ │ ├─ Bot x2        │   │  │ │ ├─ Read-only     │   │      │
│  │ │ ├─ MCP x1        │   │  │ │ ├─ WAL streamed  │   │      │
│  │ │ └─ Worker x2     │   │  │ │ └─ Auto-failover│   │      │
│  │ │                  │   │  │ │                 │   │      │
│  │ ├─ PostgreSQL x2   │   │  │ └─ Neo4j Standby  │   │      │
│  │ │  (Primary)       │   │  │ (Backup streaming)   │      │
│  │ │                  │   │  └─────────────────────┘   │      │
│  │ ├─ Neo4j Cluster   │   │                             │      │
│  │ │  (3 nodes)       │   │  ◄─── WAL Stream           │      │
│  │ │  ├─ Leader       │   │  ◄─── Backup Snapshots     │      │
│  │ │  ├─ Follower     │   │                             │      │
│  │ │  └─ Follower     │   │  S3 Backup Bucket          │      │
│  │ │                  │   │  (Every 6 hours)           │      │
│  │ └─ Redis Cache     │   │                             │      │
│  │ │  (embeddings)    │   │                             │      │
│  │ │                  │   │                             │      │
│  │ └─ Observability   │   │                             │      │
│  │    ├─ Prometheus   │   │                             │      │
│  │    ├─ Grafana      │   │                             │      │
│  │    ├─ Loki         │   │                             │      │
│  │    └─ Jaeger       │   │                             │      │
│  └────────────────────┘   └─────────────────────────────┘      │
│         │                                                      │
│         └──────────────┬─────────────────────────────          │
│                        │                                       │
│                        ▼                                       │
│                ┌────────────────┐                              │
│                │  CloudFlare    │                              │
│                │  Load Balancer │                              │
│                │  + DDoS Protect│                              │
│                └────────────────┘                              │
│                        │                                       │
│                        │ (DNS + Routing)                       │
│          ┌─────────────┴─────────────┐                         │
│          │                           │                         │
│     Telegram API              Claude.ai / Users               │
│                                                               │
└───────────────────────────────────────────────────────────────┘

Benefits:
  ✅ RTO < 5 minutes (auto-failover)
  ✅ RPO < 1 hour (WAL streaming)
  ✅ Scales to 1000+ concurrent users
  ✅ Full observability
  ✅ Multi-region resilience
  ✅ Zero data loss (synchronous replication)

Cost estimate: €3000-5000/month

Alternatives for budget:
  - Single region HA: €800-1200/month
  - Multi-region async: €1500-2000/month
```

---

*Architecture Analysis - 06 avril 2026*
*Non-modifié - Pour documentation interne*
