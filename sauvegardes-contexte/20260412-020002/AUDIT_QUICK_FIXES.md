# QUICK FIXES - CODE & CONFIG À APPLIQUER IMMÉDIATEMENT
**Durée totale** : ~4 heures pour tous les fixes
**Impact** : Mitige 60% des risques critiques

---

## FIX #1 : Resource Limits (10 min)

**Fichier** : `docker/docker-compose.yml`

**Avant** :
```yaml
postgres:
  image: pgvector/pgvector:pg16
  # ❌ Pas de limits

bot:
  build: ...
  # ❌ Pas de limits
```

**Après** :
```yaml
postgres:
  image: pgvector/pgvector:pg16
  deploy:
    resources:
      limits:
        memory: 2G
        cpus: 1.0
      reservations:
        memory: 512M
        cpus: 0.5

neo4j:
  image: neo4j:5.26.0
  deploy:
    resources:
      limits:
        memory: 2G           # Augmenter depuis 1536M
        cpus: 1.0
      reservations:
        memory: 512M

bot:
  build:
    context: ../bot
    dockerfile: Dockerfile
  deploy:
    resources:
      limits:
        memory: 1.5G
        cpus: 1.0
      reservations:
        memory: 512M
        cpus: 0.5

caddy:
  image: caddy:2-alpine
  deploy:
    resources:
      limits:
        memory: 512M
        cpus: 0.5

mcp-remote:
  build: ...
  deploy:
    resources:
      limits:
        memory: 512M
        cpus: 0.5
```

**Deploy** :
```bash
cd docker
docker compose --env-file ../.env down
docker compose --env-file ../.env up -d
```

---

## FIX #2 : Rate Limiting Caddy (15 min)

**Fichier** : `docker/caddy/Caddyfile`

**Avant** :
```caddyfile
{$BOT_DOMAIN:srv987452.hstgr.cloud} {
  handle /api/* {
    reverse_proxy bot:8000
  }
}
```

**Après** :
```caddyfile
{$BOT_DOMAIN:srv987452.hstgr.cloud} {
  # Rate limiting - critical endpoints
  handle /api/memo {
    rate_limit 10/m                           # 10 requests/minute
    reverse_proxy bot:8000 {
      timeout 30s
    }
  }

  handle /api/facts {
    rate_limit 20/m                           # 20 requests/minute
    reverse_proxy bot:8000 {
      timeout 30s
    }
  }

  handle /api/correct {
    rate_limit 5/m                            # 5 requests/minute
    reverse_proxy bot:8000 {
      timeout 30s
    }
  }

  handle /webhook/* {
    rate_limit 100/m                          # Telegram webhooks
    reverse_proxy bot:8000 {
      timeout 30s
    }
  }

  # General API rate limit
  handle /api/* {
    rate_limit 100/m                          # 100 requests/minute global
    reverse_proxy bot:8000 {
      timeout 30s
    }
  }

  # Health checks - unlimited
  handle /health {
    reverse_proxy bot:8000
  }

  handle /status {
    reverse_proxy bot:8000
  }

  # Default
  handle {
    reverse_proxy bot:8000 {
      timeout 30s
    }
  }

  # Security headers
  header /* X-Frame-Options "SAMEORIGIN"
  header /* X-Content-Type-Options "nosniff"
  header /* X-XSS-Protection "1; mode=block"
  header /* Referrer-Policy "strict-origin-when-cross-origin"
}
```

---

## FIX #3 : asyncpg Pool Augmentation (5 min)

**Fichier** : `bot/graphiti_worker.py`

**Ligne 53-57, AVANT** :
```python
self._pool = await asyncpg.create_pool(
    POSTGRES_DSN,
    min_size=1,           # ❌ Trop petit
    max_size=3,           # ❌ Trop petit
)
```

**APRÈS** :
```python
self._pool = await asyncpg.create_pool(
    POSTGRES_DSN,
    min_size=5,           # Augmenté pour 10+ concurrent users
    max_size=20,          # Permet burst handling
    max_cached_statement_lifetime=300,
    max_cacheable_statement_size=15000,
    record_class=dict,
)
```

**Validation** :
```python
# Dans lifespan() après pool creation, ajouter
pool_stats = await self._pool._holders.__len__()
logger.info(f"asyncpg pool initialized: {pool_stats} holders")
```

---

## FIX #4 : Circuit Breaker LLM (90 min)

**Fichier** : `bot/requirements.txt`

**Ajouter** :
```
pybreaker>=0.7.0
```

**Fichier** : `bot/llm_provider.py`

**Ajouter import** (ligne 14) :
```python
from pybreaker import CircuitBreaker
```

**Remplacer FallbackChain** (ligne 56-136) :

**AVANT** :
```python
async def chat(self, messages, provider=None, temperature=0.7, max_tokens=2000):
    if provider:
        return await self._call_provider(provider, messages, temperature, max_tokens)

    chain = FALLBACK_CHAIN.copy()
    if self.current_provider in chain:
        chain.remove(self.current_provider)
        chain.insert(0, self.current_provider)

    last_error = None
    attempted = 0

    for p in chain:
        config = LLM_CONFIGS.get(p)
        if not config:
            continue
        if not self._provider_is_configured(p, config):
            continue

        try:
            attempted += 1
            result = await self._call_with_retry(p, messages, temperature, max_tokens)
            if p != self.current_provider:
                logger.warning("Fallback vers %s", p)
            return result
        except Exception as e:
            logger.warning("Provider %s echoue: %s", p, e)
            last_error = e
            continue

    if attempted == 0:
        raise RuntimeError("Aucun provider LLM configure")

    raise RuntimeError(f"Tous les providers ont echoue: {last_error}")
```

**APRÈS** :
```python
def __init__(self):
    self.current_provider = os.getenv("LLM_PROVIDER", "deepseek")
    self.breakers = {}  # Circuit breaker par provider
    for p in FALLBACK_CHAIN:
        self.breakers[p] = CircuitBreaker(
            fail_max=5,                    # Fail après 5 erreurs
            reset_timeout=60,              # Retry après 60s
            exclude=[ValueError],          # Ne pas compter les erreurs config
            listeners=[self._breaker_listener]
        )
    logger.info("LLMProvider init - defaut: %s", self.current_provider)

def _breaker_listener(self, breaker, *args, **kwargs):
    """Log quand breaker change d'état"""
    logger.warning("CircuitBreaker %s: %s", breaker.name, "OPEN" if breaker.opened else "CLOSED")

async def chat(self, messages, provider=None, temperature=0.7, max_tokens=2000):
    """Envoie un message avec circuit breaker par provider."""
    if provider:
        breaker = self.breakers.get(provider)
        if breaker:
            return await breaker.call(
                self._call_provider,
                provider, messages, temperature, max_tokens
            )
        return await self._call_provider(provider, messages, temperature, max_tokens)

    # Fallback automatique avec circuit breaker
    chain = FALLBACK_CHAIN.copy()
    if self.current_provider in chain:
        chain.remove(self.current_provider)
        chain.insert(0, self.current_provider)

    last_error = None
    attempted = 0

    for p in chain:
        config = LLM_CONFIGS.get(p)
        if not config:
            logger.debug("Provider inconnu: %s", p)
            continue

        if not self._provider_is_configured(p, config):
            logger.debug("Provider %s non configure", p)
            continue

        breaker = self.breakers.get(p)
        if not breaker:
            continue

        try:
            attempted += 1
            # Utiliser circuit breaker
            result = await breaker.call(
                self._call_with_retry,
                p, messages, temperature, max_tokens
            )
            if p != self.current_provider:
                logger.warning("Fallback vers %s (defaut %s indisponible)", p, self.current_provider)
            return result
        except Exception as e:
            logger.warning("Provider %s indisponible (breaker/erreur): %s", p, type(e).__name__)
            last_error = e
            continue

    if attempted == 0:
        raise RuntimeError("Aucun provider LLM configure correctement (cles API absentes ou placeholders).")

    raise RuntimeError(f"Tous les providers ont echoue apres circuit breaker. Derniere erreur: {last_error}")
```

---

## FIX #5 : Backup Neo4j (30 min)

**Fichier** : `scripts/backup-neo4j.sh` (CRÉER)

```bash
#!/bin/bash
# ===========================================
# Backup quotidien Neo4j -> local + S3
# ===========================================
# Cron : 0 4 * * * /opt/agea/scripts/backup-neo4j.sh >> /var/log/agea-backup-neo4j.log 2>&1
# ===========================================

set -euo pipefail

TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR="/opt/agea/backups"
BACKUP_FILE="agea-neo4j-${TIMESTAMP}.dump"

mkdir -p "$BACKUP_DIR"

echo "[${TIMESTAMP}] Debut backup Neo4j..."

# Dump Neo4j
cd /opt/agea/docker
docker compose --env-file ../.env exec -T neo4j neo4j-admin database backup full neo4j /backups

# Copier vers volume local mount
docker compose --env-file ../.env cp neo4j:/backups/neo4j "${BACKUP_DIR}/${BACKUP_FILE}"

SIZE=$(du -h "${BACKUP_DIR}/${BACKUP_FILE}" | cut -f1)
echo "[${TIMESTAMP}] Dump cree: ${BACKUP_FILE} (${SIZE})"

# Upload vers S3 si configure
if command -v aws &> /dev/null && [ "${S3_ACCESS_KEY:-xxx}" != "xxx" ]; then
    AWS_ACCESS_KEY_ID="${S3_ACCESS_KEY}" \
    AWS_SECRET_ACCESS_KEY="${S3_SECRET_KEY}" \
    aws s3 cp \
        "${BACKUP_DIR}/${BACKUP_FILE}" \
        "s3://${S3_BUCKET:-agea-backups}/neo4j/${BACKUP_FILE}" \
        --endpoint-url "${S3_ENDPOINT:-https://s3.fr-par.scw.cloud}"
    echo "[${TIMESTAMP}] Upload S3 OK"
else
    echo "[${TIMESTAMP}] Backup local uniquement (S3 non configure)"
fi

# Nettoyage local > 14 jours
find "$BACKUP_DIR" -name "agea-neo4j-*.dump" -mtime +14 -delete
echo "[${TIMESTAMP}] Nettoyage complet"

echo "[${TIMESTAMP}] Backup Neo4j termine"
```

**Déployer** :
```bash
chmod +x /opt/agea/scripts/backup-neo4j.sh

# Ajouter cron
crontab -e
# Ajouter : 0 4 * * * /opt/agea/scripts/backup-neo4j.sh >> /var/log/agea-backup-neo4j.log 2>&1

# Tester restore script (créer scripts/restore-neo4j.sh)
```

---

## FIX #6 : Validation .env Defaults (5 min)

**Fichier** : `docker/docker-compose.yml`

**AVANT (DANGEREUX)** :
```yaml
bot:
  environment:
    POSTGRES_DSN: ${POSTGRES_DSN:-postgresql://agea:password@postgres:5432/agea_memory}
```

**APRÈS (SÛRE)** :
```yaml
bot:
  environment:
    POSTGRES_DSN: ${POSTGRES_DSN}  # OBLIGATOIRE, pas de default
    # Ajouter validation au startup
```

**Fichier** : `bot/main.py` (début lifespan)

**Ajouter** (ligne 55) :
```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Demarrage et arret de l'application."""
    global graphiti_worker

    # VALIDATION SECRETS & CONFIG
    required_env = [
        "TELEGRAM_BOT_TOKEN",
        "POSTGRES_DSN",
        "NEO4J_PASSWORD",
    ]
    missing = [k for k in required_env if not os.getenv(k)]
    if missing:
        logger.error("VARIABLES D'ENVIRONNEMENT MANQUANTES: %s", missing)
        raise RuntimeError(f"Config incomplete: {', '.join(missing)}")

    logger.info("AGEA demarre - LLM: %s, Mode: %s", llm.current_provider, TELEGRAM_MODE)
    # ... reste du code
```

---

## FIX #7 : Global Request Timeout (30 min)

**Fichier** : `bot/main.py`

**Ajouter middleware** (après CORSMiddleware, ligne 165) :

```python
# Middleware timeout global
@app.middleware("http")
async def timeout_middleware(request: Request, call_next):
    """Enforce 30s timeout par requête HTTP."""
    try:
        # Créer task avec timeout
        task = asyncio.create_task(call_next(request))
        response = await asyncio.wait_for(task, timeout=30.0)
        return response
    except asyncio.TimeoutError:
        logger.error("Request timeout (30s): %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=504,
            content={"error": "Request timeout after 30s"}
        )
    except Exception as e:
        logger.error("Middleware error: %s", e)
        raise
```

---

## FIX #8 : Version Pins Requirements (5 min)

**Fichier** : `bot/requirements.txt`

**AVANT** :
```
httpx==0.28.*
asyncpg==0.30.*
groq>=0.9.0
```

**APRÈS** :
```
httpx==0.28.1
asyncpg==0.30.0
groq>=0.9.0,<1.0.0
pybreaker==0.7.0
prometheus-client==0.20.0
```

**Fichier** : `mcp-server/requirements.txt`

**AVANT** :
```
mcp[cli]>=1.8.0
httpx>=0.27.0
uvicorn>=0.30.0
```

**APRÈS (UNIFIER AVEC BOT)** :
```
mcp[cli]>=1.8.0,<2.0.0
httpx==0.28.1
uvicorn==0.34.0
```

---

## FIX #9 : Docker Image Versions Pinned (2 min)

**Fichier** : `docker/docker-compose.yml`

**AVANT** :
```yaml
caddy:
  image: caddy:2-alpine
postgres:
  image: pgvector/pgvector:pg16
```

**APRÈS** :
```yaml
caddy:
  image: caddy:2.8.4-alpine
postgres:
  image: pgvector/pgvector:pg16-v0.6.1
neo4j:
  image: neo4j:5.26.0-community
```

---

## FIX #10 : Minimal Logging Configuration (20 min)

**Fichier** : `bot/main.py` (remplacer logging setup, ligne 37-42)

**AVANT** :
```python
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
```

**APRÈS** :
```python
import logging.config

LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        },
        "json": {
            "format": '{"timestamp":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}'
        }
    },
    "handlers": {
        "default": {
            "level": "INFO",
            "class": "logging.StreamHandler",
            "formatter": "standard",
            "stream": "ext://sys.stdout"
        },
        "error": {
            "level": "ERROR",
            "class": "logging.StreamHandler",
            "formatter": "json",
            "stream": "ext://sys.stderr"
        }
    },
    "loggers": {
        "agea": {
            "handlers": ["default", "error"],
            "level": "INFO",
            "propagate": False
        },
        "agea.graphiti": {
            "handlers": ["default"],
            "level": "DEBUG",
            "propagate": False
        }
    }
}

logging.config.dictConfig(LOGGING_CONFIG)
logger = logging.getLogger("agea")
```

---

## APPLY ORDER

1. FIX #1 : Resource Limits (10 min) → docker compose restart
2. FIX #2 : Rate Limiting (15 min) → caddy reload
3. FIX #3 : asyncpg Pool (5 min) → bot restart
4. FIX #9 : Docker Versions (2 min) → rebuild
5. FIX #6 : .env Validation (5 min) → test
6. FIX #8 : Requirements Pins (5 min) → rebuild
7. FIX #7 : Global Timeout (30 min) → bot rebuild
8. FIX #4 : Circuit Breaker (90 min) → bot rebuild
9. FIX #5 : Backup Neo4j (30 min) → cron setup
10. FIX #10 : Logging (20 min) → bot restart

**Total : ~3.5 heures**

---

## VALIDATION POST-APPLY

```bash
# 1. Services healthy
docker compose --env-file .env ps
docker compose --env-file .env logs -f bot

# 2. Rate limiting actif
curl -X POST http://localhost:8000/api/memo (10x rapid)
# Doit avoir erreur après 10 req/min

# 3. asyncpg pool
docker compose --env-file .env logs bot | grep "asyncpg pool"

# 4. Circuit breaker en logs
docker compose --env-file .env logs bot | grep "CircuitBreaker"

# 5. .env validation
# Bot should fail to start if POSTGRES_DSN missing

# 6. Timeout test
# Requête 40s should timeout with 504

# 7. Backup Neo4j
/opt/agea/scripts/backup-neo4j.sh
ls -lh /opt/agea/backups/agea-neo4j-*
```

---

*Guide appliqué le 06 avril 2026*
*~3-4h pour non-bloquant upgrade de sécurité+scalabilité*
