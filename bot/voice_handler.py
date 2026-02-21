"""
VoiceHandler - Transcription vocale via Groq Whisper
=====================================================
Recoit un fichier audio Telegram (.ogg), le transcrit en texte,
et detecte l'intention (memo ou correction).

Utilise Groq Whisper (whisper-large-v3-turbo) :
- ~2s pour 30s audio, quasi-gratuit, 0 RAM VPS
"""

import os
import logging
import tempfile

logger = logging.getLogger("agea.voice")

# Prompt de contexte Whisper pour termes metier
WHISPER_PROMPT = os.getenv(
    "WHISPER_PROMPT",
    "Tesla Electric, chantier, photovoltaïque, tuile romaine, tuile canal, "
    "HEXAGONE ENERGIE, QualiPV500, QualiPAC, IRVE, onduleur, Huawei, SMA, "
    "CHU Nice, appel d'offres, CCTP, BPU, DQE, sous-traitant, "
    "pompe à chaleur, gainable, mono-split, multi-split, "
    "chemin de câble, tableau divisionnaire, TGBT"
)


class VoiceHandler:
    """Transcription audio via Groq Whisper API."""

    def __init__(self):
        api_key = os.getenv("GROQ_API_KEY")
        if api_key:
            from groq import AsyncGroq
            self.client = AsyncGroq(api_key=api_key)
            self.available = True
            logger.info("VoiceHandler initialise (Groq Whisper)")
        else:
            self.client = None
            self.available = False
            logger.info("VoiceHandler desactive (GROQ_API_KEY manquante)")

    async def transcribe(self, ogg_bytes: bytes) -> str:
        """Transcrit un fichier audio via Groq Whisper."""
        if not self.available:
            raise RuntimeError("GROQ_API_KEY non configuree")

        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(ogg_bytes)
            tmp_path = tmp.name

        try:
            with open(tmp_path, "rb") as audio_file:
                transcription = await self.client.audio.transcriptions.create(
                    file=("voice.ogg", audio_file.read()),
                    model="whisper-large-v3-turbo",
                    language="fr",
                    response_format="text",
                    temperature=0.0,
                    prompt=WHISPER_PROMPT,
                )
            return transcription.strip()
        finally:
            os.unlink(tmp_path)

    # detect_intent retire — utiliser intent_detector.detect_intent() a la place
