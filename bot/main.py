"""
AGEA - Bot Telegram + API FastAPI
==================================
Source canonique unique pour la memoire inter-IA.
Recoit les messages Telegram, interroge Zep/Graphiti, repond.
"""

import os
import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from llm_provider import LLMProvider
from zep_client import ZepClient
from graphiti_client import GraphitiClient
from graphiti_worker import GraphitiWorker, enqueue_graphiti_task
from voice_handler import VoiceHandler
from intent_detector import detect_intent, detect_business_tag, format_response, tag_content
from daily_summary import DailySummary
from proactive import ProactiveAgent

# --- Configuration ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ALLOWED_USERS = os.getenv("TELEGRAM_ALLOWED_USERS", "").split(",")
ZEP_API_URL = os.getenv("ZEP_API_URL", "http://localhost:8080")
ZEP_SECRET_KEY = os.getenv("ZEP_SECRET_KEY", "")
TELEGRAM_MODE = os.getenv("TELEGRAM_MODE", "polling")  # "polling" ou "webhook"
MEHDI_CHAT_ID = os.getenv("MEHDI_CHAT_ID", "")  # Chat ID Telegram de Mehdi (Phase 6D/6E)

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("agea")

# --- LLM Provider + Zep + Graphiti + Voice ---
llm = LLMProvider()
zep = ZepClient()
graphiti = GraphitiClient()
graphiti_worker = None  # Initialise dans le lifespan
voice = VoiceHandler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Demarrage et arret de l'application."""
    global graphiti_worker
    logger.info("AGEA demarre - LLM: %s, Mode: %s", llm.current_provider, TELEGRAM_MODE)
    logger.info("Zep API: %s", ZEP_API_URL)

    # Initialiser Graphiti (non-bloquant si desactive ou echec)
    graphiti_ok = await graphiti.initialize()
    if graphiti_ok:
        logger.info("Graphiti initialise avec succes")
    else:
        logger.info("Graphiti non disponible (desactive ou echec init)")

    # Lancer le worker Graphiti en tache de fond
    worker_task = None
    if graphiti_ok:
        graphiti_worker = GraphitiWorker(graphiti)
        worker_task = asyncio.create_task(graphiti_worker.run())
        logger.info("Worker Graphiti lance en arriere-plan")

    # Lancer les crons Phase 6D/6E (resume quotidien + relances proactives)
    summary_task = None
    proactive_task = None
    if MEHDI_CHAT_ID:
        summary = DailySummary(MEHDI_CHAT_ID, send_telegram, graphiti)
        summary_task = asyncio.create_task(summary.run())
        logger.info("Cron resume quotidien lance")

        proactive = ProactiveAgent(MEHDI_CHAT_ID, send_telegram, graphiti)
        proactive_task = asyncio.create_task(proactive.run())
        logger.info("Cron relances proactives lance")
    else:
        logger.info("MEHDI_CHAT_ID non configure — crons 6D/6E desactives")

    polling_task = None
    if TELEGRAM_MODE == "polling":
        # Supprimer tout webhook existant avant de passer en polling
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/deleteWebhook"
            )
        polling_task = asyncio.create_task(telegram_polling_loop())
        logger.info("Telegram polling demarre")
    else:
        webhook_url = f"https://{os.getenv('BOT_DOMAIN', '')}/webhook/telegram"
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook",
                json={"url": webhook_url},
            )
            logger.info("Webhook configure: %s -> %s", webhook_url, resp.json())

    yield

    # Arret propre
    if graphiti_worker:
        await graphiti_worker.stop()
    if worker_task:
        worker_task.cancel()
    if polling_task:
        polling_task.cancel()
    if summary_task:
        summary_task.cancel()
    if proactive_task:
        proactive_task.cancel()
    await graphiti.close()
    logger.info("AGEA arrete")


app = FastAPI(
    title="AGEA - Memoire Inter-IA HEXAGONE ENERGIE",
    description=(
        "API de memoire partagee pour HEXAGONE ENERGIE / Tesla Electric. "
        "Permet a toute IA (ChatGPT, Gemini, Claude, etc.) de lire et ecrire "
        "dans la memoire persistante Zep via recherche semantique. "
        "Authentification par Bearer token obligatoire."
    ),
    version="1.0.0",
    lifespan=lifespan,
    servers=[{"url": "https://srv987452.hstgr.cloud", "description": "Production VPS"}],
)

# --- CORS (Phase 3 - Custom GPT / Gemini Gem) ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://chatgpt.com",
        "https://chat.openai.com",
        "https://aistudio.google.com",
        "https://gemini.google.com",
    ],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


# --- Health Check ---
@app.get("/health")
async def health():
    """Endpoint de sante pour Docker healthcheck."""
    return {
        "status": "ok",
        "service": "agea",
        "llm_provider": llm.current_provider,
        "graphiti_available": graphiti.available,
        "graphiti_read_enabled": graphiti.read_enabled,
    }


# --- Status ---
@app.get("/status")
async def status():
    """Statut detaille du systeme."""
    return {
        "service": "agea",
        "version": "3.0.0",
        "llm_provider": llm.current_provider,
        "zep_url": ZEP_API_URL,
        "telegram_configured": bool(TELEGRAM_BOT_TOKEN),
        "graphiti_available": graphiti.available,
        "graphiti_read_enabled": graphiti.read_enabled,
        "voice_available": voice.available,
    }


# --- API REST Bridge (Phase 1 - Bridge Universel Inter-IA) ---

API_TOKEN = os.getenv("ZEP_SECRET_KEY", "")


async def verify_bearer(authorization: str = Header(default="")) -> None:
    """Verifie le Bearer token pour les endpoints API REST."""
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer token requis")
    if authorization[7:] != API_TOKEN:
        raise HTTPException(status_code=403, detail="Token invalide")


class MemoRequest(BaseModel):
    """Corps de requete pour sauvegarder une information dans la memoire."""
    content: str
    role: str = "user"
    session_id: str = "mehdi-agea"

    model_config = {"json_schema_extra": {
        "examples": [{"content": "Le projet X a ete valide par le client", "role": "user"}]
    }}


@app.get("/api/context", tags=["bridge"], operation_id="searchMemory",
         summary="Recherche semantique dans la memoire")
async def api_get_context(
    q: str,
    limit: int = 5,
    session_id: str = "mehdi-agea",
    authorization: str = Header(default=""),
):
    """Cherche dans la memoire AGEA par recherche semantique.
    Appeler cette route AVANT de repondre a l'utilisateur pour recuperer
    du contexte pertinent (projets, decisions, clients, certifications).
    Retourne les resultats tries par score de pertinence."""
    await verify_bearer(authorization)
    results = await zep.search(q, limit=limit, session_id=session_id)
    filtered = [r for r in results if r["score"] > 0.3]
    return {"query": q, "results": filtered, "count": len(filtered)}


@app.post("/api/memo", tags=["bridge"], operation_id="saveMemo",
          summary="Sauvegarder une information dans la memoire")
async def api_post_memo(
    req: MemoRequest,
    authorization: str = Header(default=""),
):
    """Sauvegarde une information dans la memoire AGEA.
    Appeler cette route APRES chaque echange important pour persister
    les decisions, informations client, ou connaissances acquises."""
    await verify_bearer(authorization)
    ok = await zep.add_memory(req.content, role=req.role, session_id=req.session_id)

    # Queue Graphiti async (si active)
    queued = False
    if graphiti.available:
        queued = await enqueue_graphiti_task(
            content=req.content,
            task_type="add_episode",
            source_description=f"api ({req.role})",
        )

    return {"ok": ok, "queued": queued, "content": req.content[:100]}


@app.get("/api/session/{session_id}/history", tags=["bridge"], operation_id="getHistory",
         summary="Historique recent de la memoire")
async def api_get_history(
    session_id: str,
    last_n: int = 10,
    authorization: str = Header(default=""),
):
    """Recupere les N derniers messages de la session memoire.
    Utile pour reconstruire le contexte complet d'une conversation
    ou voir les echanges recents avec d'autres IAs."""
    await verify_bearer(authorization)
    memory = await zep.get_memory(session_id=session_id, last_n=last_n)
    return memory or {"messages": []}


# --- API REST Graphiti (Phase 5) ---

class CorrectRequest(BaseModel):
    """Corps de requete pour corriger un fait."""
    content: str
    source: str = "api"


@app.get("/api/facts", tags=["graphiti"], operation_id="searchFacts",
         summary="Recherche dans le knowledge graph (fallback Zep)")
async def api_get_facts(
    q: str,
    limit: int = 5,
    authorization: str = Header(default=""),
):
    """Recherche hybride dans le knowledge graph Graphiti.
    0 appels LLM (seulement 1 embedding) = sub-second.
    Fallback automatique vers Zep si Graphiti non disponible."""
    await verify_bearer(authorization)

    source = "zep"
    results = []

    if graphiti.read_enabled:
        facts = await graphiti.search(q, num_results=limit)
        if facts:
            source = "graphiti"
            results = [{"fact": f["fact"], "name": f.get("name", "")} for f in facts]

    if not results:
        zep_results = await zep.search(q, limit=limit)
        results = [
            {"fact": r["content"], "name": "", "score": r["score"]}
            for r in zep_results if r["score"] > 0.3
        ]

    return {"query": q, "source": source, "results": results, "count": len(results)}


@app.post("/api/correct", tags=["graphiti"], operation_id="correctFact",
          summary="Corriger un fait dans le knowledge graph")
async def api_post_correct(
    req: CorrectRequest,
    authorization: str = Header(default=""),
):
    """Enqueue une correction bi-temporelle.
    Le worker Graphiti traitera la correction en arriere-plan."""
    await verify_bearer(authorization)

    if not graphiti.available:
        raise HTTPException(status_code=503, detail="Graphiti non disponible")

    queued = await enqueue_graphiti_task(
        content=req.content,
        task_type="correct",
        source_description=req.source,
    )
    return {"ok": queued, "content": req.content[:100]}


@app.get("/api/entity/{name}", tags=["graphiti"], operation_id="getEntity",
         summary="Recuperer les faits lies a une entite")
async def api_get_entity(
    name: str,
    authorization: str = Header(default=""),
):
    """Recupere tous les faits lies a une entite dans le knowledge graph."""
    await verify_bearer(authorization)

    if not graphiti.read_enabled:
        raise HTTPException(status_code=503, detail="Lecture Graphiti non activee")

    entity = await graphiti.get_entity(name)
    return entity or {"entity": name, "facts_count": 0, "facts": []}


@app.get("/api/graphiti/health", tags=["graphiti"], operation_id="graphitiHealth",
         summary="Sante du knowledge graph Graphiti/Neo4j")
async def api_graphiti_health(
    authorization: str = Header(default=""),
):
    """Verifie la sante de Graphiti et tente reconnexion si necessaire."""
    await verify_bearer(authorization)
    return await graphiti.health_check()


@app.get("/api/graphiti/queue", tags=["graphiti"], operation_id="graphitiQueue",
         summary="Statistiques de la queue Graphiti")
async def api_graphiti_queue(
    authorization: str = Header(default=""),
):
    """Retourne les statistiques de la queue de traitement."""
    await verify_bearer(authorization)

    if not graphiti_worker:
        return {"error": "Worker non actif", "pending": 0, "done": 0, "failed": 0, "total": 0}

    return await graphiti_worker.get_queue_stats()


@app.get("/api/stats/today", tags=["stats"], operation_id="statsToday",
         summary="Statistiques du jour (memos, tags, corrections)")
async def api_stats_today(
    authorization: str = Header(default=""),
):
    """Retourne les stats du jour pour le resume quotidien."""
    await verify_bearer(authorization)
    summary = DailySummary("", send_telegram, graphiti)
    stats = await summary._get_today_stats()
    return stats


@app.post("/api/admin/reindex", tags=["admin"], operation_id="adminReindex",
          summary="Re-enqueue les messages Zep dans Graphiti")
async def api_admin_reindex(
    last_n: int = 50,
    authorization: str = Header(default=""),
):
    """Relit les N derniers messages Zep et les enqueue pour Graphiti.
    Idempotent grace a message_uuid UNIQUE + ON CONFLICT DO NOTHING."""
    await verify_bearer(authorization)

    if not graphiti.available:
        raise HTTPException(status_code=503, detail="Graphiti non disponible")

    memory = await zep.get_memory(session_id="mehdi-agea", last_n=last_n)
    messages = memory.get("messages", []) if memory else []

    enqueued = 0
    for msg in messages:
        content = msg.get("content", "")
        if content and len(content) > 10:
            ok = await enqueue_graphiti_task(
                content=content,
                task_type="add_episode",
                source_description="reindex zep",
            )
            if ok:
                enqueued += 1

    return {"ok": True, "messages_found": len(messages), "enqueued": enqueued}


# --- Telegram Polling ---

async def telegram_polling_loop():
    """Boucle de polling pour recevoir les messages Telegram sans webhook."""
    offset = 0
    async with httpx.AsyncClient(timeout=35.0) as client:
        while True:
            try:
                resp = await client.get(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates",
                    params={"offset": offset, "timeout": 30},
                )
                data = resp.json()
                for update in data.get("result", []):
                    offset = update["update_id"] + 1
                    await process_telegram_update(update)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Erreur polling: %s", e)
                await asyncio.sleep(5)


async def process_telegram_update(update: dict):
    """Traite un update Telegram (depuis polling ou webhook)."""
    message = update.get("message", {})
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = message.get("text", "")
    user_id = str(message.get("from", {}).get("id", ""))

    if TELEGRAM_ALLOWED_USERS and user_id not in TELEGRAM_ALLOWED_USERS:
        logger.warning("Utilisateur non autorise: %s", user_id)
        return

    # Message vocal ou audio
    voice_data = message.get("voice") or message.get("audio")
    if voice_data:
        logger.info("Message vocal de %s (%ds)", user_id, voice_data.get("duration", 0))
        await handle_voice(chat_id, voice_data)
        return

    if not text:
        return

    logger.info("Message de %s: %s", user_id, text[:100])

    if text.startswith("/"):
        await handle_command(chat_id, text)
    else:
        await handle_message(chat_id, text)


# --- Webhook Telegram ---
@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    """
    Recoit les messages Telegram via webhook.
    Pattern : reponse immediate 'En cours...' puis editMessageText apres traitement.
    """
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="JSON invalide")

    await process_telegram_update(data)
    return JSONResponse({"ok": True})


async def handle_command(chat_id: str, text: str):
    """Route les commandes Telegram."""
    command = text.split()[0].lower()
    args = text[len(command):].strip()

    handlers = {
        "/start": cmd_start,
        "/status": cmd_status,
        "/ask": cmd_ask,
        "/memo": cmd_memo,
        "/projet": cmd_projet,
        "/correct": cmd_correct,
        "/forget": cmd_forget,
        "/entity": cmd_entity,
        "/queue": cmd_queue,
    }

    handler = handlers.get(command)
    if handler:
        await handler(chat_id, args)
    else:
        await send_telegram(chat_id, f"Commande inconnue: {command}")


async def handle_message(chat_id: str, text: str):
    """Traite un message texte libre par detection d'intention (Phase 6A)."""
    intent = detect_intent(text)
    logger.info("Intent detecte: %s pour: %s", intent, text[:80])

    if intent == "question":
        await cmd_ask(chat_id, text)
    elif intent == "correction":
        await route_memo(chat_id, text, intent="correction")
    elif intent == "forget":
        await route_memo(chat_id, text, intent="forget")
    else:
        await route_memo(chat_id, text, intent="memo")


async def route_memo(chat_id: str, text: str, intent: str = "memo", vocal: bool = False):
    """Route un memo/correction/forget avec tags metier (Phase 6B)."""
    tag = detect_business_tag(text)
    tagged = tag_content(text, tag)

    try:
        if intent == "correction":
            if graphiti.available:
                await enqueue_graphiti_task(
                    content=text,
                    task_type="correct",
                    source_description=f"telegram-{'vocal' if vocal else 'texte'}-correction",
                )
            await zep.add_memory(f"[CORRECTION] {tagged}", role="user")
        elif intent == "forget":
            if graphiti.available:
                await enqueue_graphiti_task(
                    content=text,
                    task_type="forget",
                    source_description=f"telegram-{'vocal' if vocal else 'texte'}-forget",
                )
            await zep.add_memory(f"[FORGET] {tagged}", role="user")
        else:
            await zep.add_memory(tagged, role="user")
            if graphiti.available:
                await enqueue_graphiti_task(
                    content=tagged,
                    task_type="add_episode",
                    source_description=f"telegram-{'vocal' if vocal else 'texte'}-{tag or 'info'}",
                )

        response = format_response(intent, tag, text, vocal=vocal)
        await send_telegram(chat_id, response)
    except Exception as e:
        logger.error("Erreur route_memo: %s", e)
        await send_telegram(chat_id, f"Erreur: {e}")


async def handle_voice(chat_id: str, voice_data: dict):
    """Traite un message vocal Telegram : transcription + memo/correct."""
    if not voice.available:
        await send_telegram(chat_id, "Transcription vocale non configuree (GROQ_API_KEY manquante).")
        return

    # 1. Feedback immediat
    processing_msg_id = await send_telegram(chat_id, "Transcription en cours...", return_message_id=True)

    try:
        # 2. Telecharger le fichier vocal via l'API Telegram
        file_id = voice_data.get("file_id")
        async with httpx.AsyncClient() as client:
            # Obtenir le file_path
            resp = await client.get(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getFile",
                params={"file_id": file_id},
            )
            file_path = resp.json().get("result", {}).get("file_path", "")
            if not file_path:
                await edit_telegram(chat_id, processing_msg_id, "Erreur: impossible de recuperer le fichier vocal.")
                return

            # Telecharger le fichier
            resp = await client.get(
                f"https://api.telegram.org/file/bot{TELEGRAM_BOT_TOKEN}/{file_path}"
            )
            ogg_bytes = resp.content

        # 3. Transcrire via Groq Whisper
        text = await voice.transcribe(ogg_bytes)

        if not text or len(text.strip()) < 3:
            await edit_telegram(chat_id, processing_msg_id, "Transcription vide. Reessayez en parlant plus fort.")
            return

        logger.info("Vocal transcrit: %s", text[:100])

        # 4. Detecter l'intention via intent_detector (Phase 6A)
        intent = detect_intent(text)
        tag = detect_business_tag(text)
        tagged = tag_content(text, tag)
        logger.info("Vocal intent: %s, tag: %s", intent, tag)

        # 5. Traiter selon l'intention
        if intent == "question":
            # Supprimer le message "Transcription..." et repondre via cmd_ask
            await edit_telegram(chat_id, processing_msg_id, f"\U0001f3a4 \"{text}\"\n\n\U0001f50d Recherche en cours...")
            await cmd_ask(chat_id, text)
            return

        if intent == "correction":
            if graphiti.available:
                await enqueue_graphiti_task(
                    content=text,
                    task_type="correct",
                    source_description=f"vocal-correction",
                )
            await zep.add_memory(f"[CORRECTION] {tagged}", role="user")
        elif intent == "forget":
            if graphiti.available:
                await enqueue_graphiti_task(
                    content=text,
                    task_type="forget",
                    source_description=f"vocal-forget",
                )
            await zep.add_memory(f"[FORGET] {tagged}", role="user")
        else:
            await zep.add_memory(f"[VOCAL] {tagged}", role="user")
            if graphiti.available:
                await enqueue_graphiti_task(
                    content=tagged,
                    task_type="add_episode",
                    source_description=f"vocal-{tag or 'info'}",
                )

        # 6. Reponse formatee avec le texte transcrit
        response = format_response(intent, tag, text, vocal=True)
        await edit_telegram(chat_id, processing_msg_id, response)

    except Exception as e:
        logger.error("Erreur transcription vocale: %s", e)
        await edit_telegram(chat_id, processing_msg_id, f"Erreur transcription: {str(e)[:100]}")


# --- Commandes ---

async def cmd_start(chat_id: str, args: str):
    """Commande /start - Bienvenue."""
    voice_status = "actif" if voice.available else "inactif"
    await send_telegram(
        chat_id,
        "AGEA v3.0 - Memoire Inter-IA\n\n"
        "Parlez-moi naturellement :\n"
        "\U0001f4dd Ecrivez une info \u2192 memorisee\n"
        "\u2753 Posez une question \u2192 reponse\n"
        "\u270f\ufe0f \"Finalement...\" \u2192 correction\n"
        "\U0001f5d1\ufe0f \"Oublie...\" \u2192 suppression\n"
        "\U0001f3a4 Envoyez un vocal \u2192 transcrit + route\n\n"
        "Commandes rapides :\n"
        "/memo /ask /correct /forget /entity /projet /queue /status\n\n"
        f"Vocal: {voice_status}",
    )


async def cmd_status(chat_id: str, args: str):
    """Commande /status - Etat du systeme."""
    graphiti_status = "ON" if graphiti.available else "OFF"
    graphiti_read = "ON" if graphiti.read_enabled else "OFF"
    voice_status = "ON" if voice.available else "OFF"
    await send_telegram(
        chat_id,
        f"AGEA v3.0.0\n"
        f"LLM: {llm.current_provider}\n"
        f"Zep: {ZEP_API_URL}\n"
        f"Graphiti: {graphiti_status} (lecture: {graphiti_read})\n"
        f"Vocal: {voice_status} (Groq Whisper)\n"
        f"Status: Operationnel",
    )


async def cmd_ask(chat_id: str, args: str):
    """Commande /ask - Interroger la memoire via Graphiti/Zep + LLM."""
    if not args:
        await send_telegram(chat_id, "Usage: /ask &lt;ta question&gt;")
        return

    try:
        context_parts = []
        source_tag = "Zep"

        # 1. Recherche Graphiti prioritaire (0 appels LLM, sub-second)
        if graphiti.read_enabled:
            graphiti_facts = await graphiti.search(args, num_results=5)
            for f in graphiti_facts:
                fact_text = f.get("fact", "")
                if fact_text:
                    context_parts.append(fact_text)
            if context_parts:
                source_tag = "Graphiti"

        # 2. Fallback Zep si pas de resultats Graphiti
        if not context_parts:
            zep_results = await zep.search(args, limit=5)
            for r in zep_results:
                if r["content"] and r["score"] > 0.5 and r["content"] != args:
                    context_parts.append(r["content"])
            source_tag = "Zep"

        # 3. Construire le prompt avec contexte memoire
        system_prompt = (
            "Tu es AGEA, l'assistant memoire de HEXAGONE ENERGIE (anciennement HEXAGON ENR). "
            "Reponds en francais.\n"
            "REGLE ABSOLUE: Le nom officiel est 'HEXAGONE ENERGIE'. "
            "Ne jamais utiliser 'HEXAGON ENR' dans tes reponses, "
            "meme si le contexte memoire contient cette ancienne appellation."
        )
        if context_parts:
            memory_context = "\n---\n".join(context_parts)
            system_prompt += (
                f"\n\nVoici le contexte pertinent de ta memoire:\n"
                f"---\n{memory_context}\n---\n"
                f"Utilise ce contexte pour repondre si pertinent."
            )

        # 4. Appeler le LLM
        response = await llm.chat(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": args},
            ]
        )

        # 5. Post-traitement: corriger noms obsoletes
        response = response.replace("HEXAGON ENR", "HEXAGONE ENERGIE")
        response = response.replace("Hexagon ENR", "Hexagone Energie")
        response = response.replace("hexagon enr", "Hexagone Energie")

        # 6. Sauvegarder l'echange dans Zep
        await zep.add_memory(args, role="user")
        await zep.add_memory(response, role="assistant")

        # 7. Ajouter indicateur source memoire
        if context_parts:
            response = f"[{source_tag}: {len(context_parts)} ref.]\n\n{response}"

        await send_telegram(chat_id, response)
    except Exception as e:
        logger.error("Erreur /ask: %s", e)
        await send_telegram(chat_id, f"Erreur: {e}")


async def cmd_memo(chat_id: str, args: str):
    """Commande /memo - Enregistrer dans Zep (sync) + queue Graphiti (async)."""
    if not args:
        await send_telegram(chat_id, "Usage: /memo &lt;information a retenir&gt;")
        return

    try:
        # 1. Zep sync (reponse immediate)
        ok = await zep.add_memory(args, role="user")

        # 2. Queue Graphiti async (si active)
        queued = False
        if graphiti.available:
            queued = await enqueue_graphiti_task(
                content=args,
                task_type="add_episode",
                source_description="telegram /memo",
            )

        if ok:
            msg = "Memo enregistre."
            if queued:
                msg += " Structuration KG en cours..."
            await send_telegram(chat_id, msg)
        else:
            await send_telegram(chat_id, "Erreur lors de l'enregistrement.")
    except Exception as e:
        logger.error("Erreur /memo: %s", e)
        await send_telegram(chat_id, f"Erreur: {e}")


async def cmd_projet(chat_id: str, args: str):
    """Commande /projet - Contexte d'un projet via Graphiti."""
    if not args:
        await send_telegram(chat_id, "Usage: /projet &lt;nom du projet&gt;")
        return

    if graphiti.read_enabled:
        entity = await graphiti.get_entity(args)
        if entity and entity.get("facts"):
            facts_text = "\n".join(
                f"- {f['fact']}" for f in entity["facts"][:10]
            )
            await send_telegram(
                chat_id,
                f"Projet '{args}' ({entity['facts_count']} faits):\n\n{facts_text}",
            )
            return

    await send_telegram(chat_id, f"Aucune info trouvee pour '{args}'.")


async def cmd_correct(chat_id: str, args: str):
    """Commande /correct - Corriger un fait dans le knowledge graph."""
    if not args:
        await send_telegram(chat_id, "Usage: /correct &lt;fait corrige&gt;")
        return

    if not graphiti.available:
        await send_telegram(chat_id, "Graphiti non disponible.")
        return

    try:
        queued = await enqueue_graphiti_task(
            content=args,
            task_type="correct",
            source_description="telegram /correct",
        )
        if queued:
            await send_telegram(chat_id, "Correction enregistree. Traitement en cours...")
        else:
            await send_telegram(chat_id, "Erreur lors de l'enregistrement de la correction.")
    except Exception as e:
        logger.error("Erreur /correct: %s", e)
        await send_telegram(chat_id, f"Erreur: {e}")


async def cmd_forget(chat_id: str, args: str):
    """Commande /forget - Invalider un fait dans le knowledge graph."""
    if not args:
        await send_telegram(chat_id, "Usage: /forget &lt;fait a oublier&gt;")
        return

    if not graphiti.available:
        await send_telegram(chat_id, "Graphiti non disponible.")
        return

    try:
        queued = await enqueue_graphiti_task(
            content=args,
            task_type="forget",
            source_description="telegram /forget",
        )
        if queued:
            await send_telegram(chat_id, "Demande d'oubli enregistree. Traitement en cours...")
        else:
            await send_telegram(chat_id, "Erreur lors de l'enregistrement.")
    except Exception as e:
        logger.error("Erreur /forget: %s", e)
        await send_telegram(chat_id, f"Erreur: {e}")


async def cmd_entity(chat_id: str, args: str):
    """Commande /entity - Recuperer les faits lies a une entite."""
    if not args:
        await send_telegram(chat_id, "Usage: /entity &lt;nom&gt;")
        return

    if not graphiti.read_enabled:
        await send_telegram(chat_id, "Lecture Graphiti non activee.")
        return

    try:
        entity = await graphiti.get_entity(args)
        if entity and entity.get("facts"):
            facts_text = "\n".join(
                f"- {f['fact']}" for f in entity["facts"][:10]
            )
            await send_telegram(
                chat_id,
                f"Entite '{args}' ({entity['facts_count']} faits):\n\n{facts_text}",
            )
        else:
            await send_telegram(chat_id, f"Aucun fait trouve pour '{args}'.")
    except Exception as e:
        logger.error("Erreur /entity: %s", e)
        await send_telegram(chat_id, f"Erreur: {e}")


async def cmd_queue(chat_id: str, args: str):
    """Commande /queue - Statut de la queue Graphiti."""
    if not graphiti_worker:
        await send_telegram(chat_id, "Worker Graphiti non actif.")
        return

    try:
        stats = await graphiti_worker.get_queue_stats()
        if "error" in stats:
            await send_telegram(chat_id, f"Erreur queue: {stats['error']}")
        else:
            await send_telegram(
                chat_id,
                f"Queue Graphiti:\n"
                f"- En attente: {stats['pending']}\n"
                f"- En cours: {stats['processing']}\n"
                f"- Terminees: {stats['done']}\n"
                f"- Echouees: {stats['failed']}\n"
                f"- Total: {stats['total']}",
            )
    except Exception as e:
        logger.error("Erreur /queue: %s", e)
        await send_telegram(chat_id, f"Erreur: {e}")


# --- Utilitaires Telegram ---

async def send_telegram(chat_id: str, text: str, return_message_id: bool = False):
    """Envoie un message Telegram. Retourne le message_id si demande."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        })
        if return_message_id:
            data = resp.json()
            return data.get("result", {}).get("message_id")


async def edit_telegram(chat_id: str, message_id: int, text: str):
    """Edite un message Telegram existant."""
    if not message_id:
        await send_telegram(chat_id, text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
        })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
