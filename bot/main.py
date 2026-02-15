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
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

from llm_provider import LLMProvider

# --- Configuration ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ALLOWED_USERS = os.getenv("TELEGRAM_ALLOWED_USERS", "").split(",")
ZEP_API_URL = os.getenv("ZEP_API_URL", "http://localhost:8080")
ZEP_SECRET_KEY = os.getenv("ZEP_SECRET_KEY", "")
TELEGRAM_MODE = os.getenv("TELEGRAM_MODE", "polling")  # "polling" ou "webhook"

# --- Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("agea")

# --- LLM Provider ---
llm = LLMProvider()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Demarrage et arret de l'application."""
    logger.info("AGEA demarre - LLM: %s, Mode: %s", llm.current_provider, TELEGRAM_MODE)
    logger.info("Zep API: %s", ZEP_API_URL)

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

    if polling_task:
        polling_task.cancel()
    logger.info("AGEA arrete")


app = FastAPI(
    title="AGEA - Memoire Inter-IA",
    version="0.1.0",
    lifespan=lifespan,
)


# --- Health Check ---
@app.get("/health")
async def health():
    """Endpoint de sante pour Docker healthcheck."""
    return {
        "status": "ok",
        "service": "agea",
        "llm_provider": llm.current_provider,
    }


# --- Status ---
@app.get("/status")
async def status():
    """Statut detaille du systeme."""
    return {
        "service": "agea",
        "version": "0.1.0",
        "llm_provider": llm.current_provider,
        "zep_url": ZEP_API_URL,
        "telegram_configured": bool(TELEGRAM_BOT_TOKEN),
    }


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
    }

    handler = handlers.get(command)
    if handler:
        await handler(chat_id, args)
    else:
        await send_telegram(chat_id, f"Commande inconnue: {command}")


async def handle_message(chat_id: str, text: str):
    """Traite un message texte libre (equivalent de /ask)."""
    await cmd_ask(chat_id, text)


# --- Commandes ---

async def cmd_start(chat_id: str, args: str):
    """Commande /start - Bienvenue."""
    await send_telegram(
        chat_id,
        "AGEA - Memoire Inter-IA\n\n"
        "Commandes disponibles:\n"
        "/ask <question> - Interroger la memoire\n"
        "/memo <texte> - Enregistrer une information\n"
        "/projet <nom> - Contexte d'un projet\n"
        "/status - Etat du systeme",
    )


async def cmd_status(chat_id: str, args: str):
    """Commande /status - Etat du systeme."""
    await send_telegram(
        chat_id,
        f"AGEA v0.1.0\n"
        f"LLM: {llm.current_provider}\n"
        f"Zep: {ZEP_API_URL}\n"
        f"Status: Operationnel",
    )


async def cmd_ask(chat_id: str, args: str):
    """Commande /ask - Interroger la memoire via Zep + LLM."""
    if not args:
        await send_telegram(chat_id, "Usage: /ask <ta question>")
        return

    # TODO: Chercher dans Zep/Graphiti d'abord
    # context = await search_zep(args)

    # Pour l'instant, reponse directe via LLM
    try:
        response = await llm.chat(
            messages=[
                {"role": "system", "content": "Tu es AGEA, l'assistant memoire d'HEXAGON ENR. Reponds en francais."},
                {"role": "user", "content": args},
            ]
        )
        await send_telegram(chat_id, response)
    except Exception as e:
        logger.error("Erreur LLM: %s", e)
        await send_telegram(chat_id, f"Erreur: {e}")


async def cmd_memo(chat_id: str, args: str):
    """Commande /memo - Enregistrer une information dans Zep."""
    if not args:
        await send_telegram(chat_id, "Usage: /memo <information a retenir>")
        return

    # TODO: Sauvegarder dans Zep/Graphiti
    # await save_to_zep(args)

    await send_telegram(chat_id, f"Memo enregistre (TODO: integration Zep)")


async def cmd_projet(chat_id: str, args: str):
    """Commande /projet - Contexte d'un projet specifique."""
    if not args:
        await send_telegram(chat_id, "Usage: /projet <nom du projet>")
        return

    # TODO: Chercher le projet dans le graphe Graphiti
    # project_context = await search_project(args)

    await send_telegram(chat_id, f"Projet '{args}' (TODO: integration Graphiti)")


# --- Utilitaires Telegram ---

async def send_telegram(chat_id: str, text: str):
    """Envoie un message Telegram."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
        })


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
