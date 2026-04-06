# RECOMMANDATIONS EXÉCUTIVES - AGEA
## Priorisation Actions Urgentes
**Date** : 06 avril 2026

---

## VUE GLOBALE RISQUE

| SPOF | Impact | Coût Fix | Durée | Urgence |
|------|--------|----------|-------|---------|
| **Neo4j pas backupé** | Perte mémoire irremplaçable | 1-2h | 1 jour | 🔴 CRITIQUE |
| **PostgreSQL pas répliqué** | Perte conversations/queue | 4-8h | 2-3 jours | 🔴 CRITIQUE |
| **Zéro monitoring** | Catastrophe silencieuse | 4h | 1-2 jours | 🔴 CRITIQUE |
| **Pas circuit breaker LLM** | Timeout 60s+ par requête | 2h | 1 jour | 🔴 CRITIQUE |
| **Pas rate limiting API** | Abuse possible | 1h | 1 jour | 🟠 HAUTE |
| **Asyncpg pool trop petit** | Blocage à 3+ users concurrent | 0.5h | < 1h | 🟠 HAUTE |
| **Pas de tests** | Regressions invisibles | 8-10h | 1 semaine | 🟡 MOYENNE |

---

## ACTION PLAN SEMAINE 1

### Lundi (4h)

**Task 1 : Backup Neo4j** (1h30)
```bash
# Fichier : scripts/backup-neo4j.sh (CRÉER)
docker exec agea-neo4j-1 neo4j-admin database backup full neo4j /backups/neo4j-$(date +%Y%m%d).dump

# Activer cron : 0 3 * * * /opt/agea/scripts/backup-neo4j.sh
```
**Owner** : Mehdi ou DevOps
**Validation** : Test restore sur backup ancien

**Task 2 : Monitoring Stack** (2h30)
- Ajouter Prometheus + Grafana au docker-compose
- Créer 3 dashboards : Services health, LLM metrics, Queue status
- Configurer 5 alertes critiques : PostgreSQL down, Neo4j down, Worker hung, Queue > 100, Memory > 80%

**Files à modifier** :
- `docker/docker-compose.yml` : +prometheus, +grafana, +loki
- `bot/main.py` : +prometheus_client metrics (30 lignes)
- `docker/prometheus.yml` (NEW)
- `docker/grafana/dashboards/` (NEW - 3 fichiers JSON)

### Mardi (3h)

**Task 3 : Circuit Breaker LLM** (2h)
```python
# Fichier : bot/llm_provider.py
# Remplacer loop retry par
from pybreaker import CircuitBreaker

self.breaker = CircuitBreaker(fail_max=5, reset_timeout=60)
result = await self.breaker.call(self._call_with_retry, ...)
```

**Task 4 : Rate Limiting Caddy** (1h)
```caddyfile
# docker/caddy/Caddyfile
rate_limit /api/memo 10/m
rate_limit /api/* 100/m
```

### Mercredi-Jeudi (4h)

**Task 5 : Resource Limits** (0.5h)
```yaml
# docker-compose.yml
postgres:
  deploy:
    resources:
      limits:
        memory: 2G
        cpus: 1

bot:
  deploy:
    resources:
      limits:
        memory: 1.5G
        cpus: 1
```

**Task 6 : PostgreSQL Replication Setup** (3h)
- Activer WAL archiving
- Configurer standby replica
- Test failover
- Script recovery documenté

---

## ACTION PLAN MOIS 1

### Week 2-3 : Testing + Scalability

1. **asyncpg pool augmentation** (30 min)
   ```python
   min_size=5
   max_size=20
   ```

2. **Global timeout endpoints** (1h)
   ```python
   # main.py : middleware qui enforce 30s timeout par requête
   @app.middleware("http")
   async def timeout_middleware(request: Request, call_next):
       task = asyncio.create_task(call_next(request))
       return await asyncio.wait_for(task, timeout=30)
   ```

3. **Integration tests** (8h)
   - 20+ test cases pour critical paths
   - Telegram message → LLM → storage
   - Graphiti queue processing
   - Fallback LLM scenario

4. **Load testing** (4h)
   - Simulator 10 utilisateurs concurrent
   - Vérifier no bottleneck avant 5-10 messages/sec

### Week 4 : Documentation + Hardening

1. **Disaster Recovery Plan** (2h)
   - How to restore PostgreSQL from backup
   - How to restore Neo4j from backup
   - How to failover to standby
   - RTO/RPO targets

2. **Security audit** (2h)
   - Vérifier pas de secrets en logs
   - Audit API endpoints authentication
   - Review Caddy security headers

3. **API Versioning** (2h)
   ```python
   # bot/main.py
   @app.post("/api/v1/memo")
   @app.get("/api/v1/facts")
   ```

---

## BUDGET TOTAL

### Semaine 1 : ~12 heures
- 1 DevOps/SRE ou Mehdi + 1 dev

### Mois 1 : ~30-35 heures
- 1 DevOps (20h) + 1 dev backend (15h)

### Avant production scale à 10+ users : ~60 heures
- PostgreSQL replication : 8h
- Kubernetes : 20h
- Monitoring avancé : 8h
- Tests exhaustifs : 10h
- Documentation : 6h

---

## QUICK WINS (< 30 min chacun)

1. ✅ **asyncpg pool** : min_size=5, max_size=20
2. ✅ **Resource limits** : Ajouter CPU/RAM cap
3. ✅ **Rate limiting** : 3 lignes Caddyfile
4. ✅ **Backup validation** : Script test restore
5. ✅ **Timeout global** : Middleware 30s

**Total : 2.5 heures pour 5 mitigations moyenne-haute criticité**

---

## POUR MEHDI : Questions Clés

### Q1 : Combien d'utilisateurs concurrents attendus en Y1 ?
- **< 5 users** : Fixes semaine 1 suffisent
- **5-20 users** : + PostgreSQL replication obligatoire (mois 1)
- **> 20 users** : Kubernetes needed (mois 2-3)

### Q2 : Quel SLA/RTO acceptable ?
- **SLA 99% (7h downtime/mois)** : Replication Primary/Standby suffit
- **SLA 99.9% (43min downtime/mois)** : Multi-région + load balancer
- **SLA 99.99% (4min downtime/mois)** : Kubernetes + Istio

### Q3 : Budget disponible ?
- **Minimaliste (< €500/mois)** : VPS Hostinger + Prometheus + scaling via asyncpg
- **Standard (€1000-2000/mois)** : + PostgreSQL managed service (Scaleway Database)
- **Premium (€3000+/mois)** : + Kubernetes (Scaleway Kapsule) + multi-region

---

## NEXT STEPS

1. **Valider avec Mehdi** : Quels users concurrent attendus ?
2. **Planifier semaine 1** : Assigner tasks (Mehdi ou DevOps externe)
3. **Ci-joint audit complet** : AUDIT_INFRASTRUCTURE.md (40 pages)
4. **Template monitoring** : À configurer (Prometheus.yml, dashboards JSON)

---

## METRICS À TRACKER

### Post-Implémentation

| Métrique | Avant | Cible | Timeline |
|----------|-------|-------|----------|
| **MTTR PostgreSQL** | Inconnu | < 15 min | Semaine 2 |
| **MTTR Neo4j** | Inconnu | < 30 min | Semaine 2 |
| **Max concurrent users** | 3-5 | 10-15 | Mois 1 |
| **p95 latency** | Inconnu | < 5s | Mois 1 |
| **Uptime** | Inconnu | 99% | Mois 1 |
| **Alertes responded < 5 min** | N/A | 100% | Mois 1 |

---

*Rapport préparé par Claude Code - 06 avril 2026*
*Non-modifié. Prêt pour présentation Mehdi.*
