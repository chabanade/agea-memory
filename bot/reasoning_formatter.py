"""
reasoning_formatter — Templates + extraction hybride + scoring (Phase 7)
========================================================================
Formate les entites de raisonnement (Decision, Doute, Apprentissage) :
1. Extraction heuristique (regex/split) — zero LLM, zero latence
2. Si score faible → LLM fallback (DeepSeek ~0.001$)
3. Validation Pydantic → score de confiance
4. Template texte enrichi → Graphiti add_episode()
"""

import json
import logging
import os
import re
from datetime import datetime

from reasoning_models import ApprentissageInput, DecisionInput, DouteInput

logger = logging.getLogger("agea.reasoning")

REVIEW_THRESHOLD = float(os.getenv("REVIEW_THRESHOLD", "0.80"))


# ---------------------------------------------------------------------------
# Templates texte enrichi (envoyes a Graphiti via add_episode)
# ---------------------------------------------------------------------------

TEMPLATES = {
    "decision": (
        "D\u00c9CISION prise le {date} [ID:{decision_id}]\n"
        "Choix : {choix}\n"
        "Contexte : {contexte}\n"
        "Justification : {justification}\n"
        "Alternatives \u00e9cart\u00e9es : {alternatives}\n"
        "Source : {source_type}{source_ref_suffix}\n"
        "Statut : {statut}"
    ),
    "doute": (
        "DOUTE enregistr\u00e9 le {date} [ID:{doute_id}]\n"
        "Question : {question}\n"
        "Contexte : {contexte}\n"
        "Statut : {statut}"
    ),
    "lecon": (
        "LE\u00c7ON retenue le {date} [ID:{apprentissage_id}]\n"
        "Erreur : {erreur}\n"
        "Correction : {correction}\n"
        "R\u00e8gle extraite : {regle_extraite}\n"
        "Source : {source_type}{source_ref_suffix}"
    ),
}


# ---------------------------------------------------------------------------
# Heuristique d'extraction (regex / split)
# ---------------------------------------------------------------------------

_DECISION_SPLIT = [
    (r"\s+car\s+", "justification"),
    (r"\s+parce\s+qu[e\u2019']\s*", "justification"),
    (r"\s+puisqu[e\u2019']\s*", "justification"),
    (r"\s+plut[\u00f4o]t\s+que?\s+", "alternatives"),
    (r"\s+au\s+lieu\s+de?\s+", "alternatives"),
    (r"\s+et\s+pas\s+", "alternatives"),
]

_LECON_SPLIT = [
    (r"\s+donc\s+", "correction"),
    (r"\s+alors\s+", "correction"),
    (r"\s+maintenant\s+", "correction"),
    (r"\s+dor[\u00e9e]navant\s+", "correction"),
    (r",?\s+faut\s+", "correction"),
    (r",?\s+il\s+faut\s+", "correction"),
]


def extract_heuristic(text: str, tag: str) -> dict:
    """Extraction heuristique par regex/split. Zero LLM."""
    text = text.strip()
    if tag == "decision":
        return _extract_decision(text)
    if tag == "doute":
        return _extract_doute(text)
    if tag == "lecon":
        return _extract_lecon(text)
    return {}


def _extract_decision(text: str) -> dict:
    data = {"choix": text, "justification": "", "alternatives": []}

    for pattern, field in _DECISION_SPLIT:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            before = text[: match.start()].strip()
            after = text[match.end() :].strip()
            if field == "justification":
                data["choix"] = before
                data["justification"] = after
            elif field == "alternatives":
                if data["choix"] == text:
                    data["choix"] = before
                data["alternatives"] = [a.strip() for a in after.split(",") if a.strip()]
            break

    return data


def _extract_doute(text: str) -> dict:
    return {"question": text, "contexte": ""}


def _extract_lecon(text: str) -> dict:
    data = {"erreur": text, "correction": "", "regle_extraite": ""}

    for pattern, _ in _LECON_SPLIT:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            data["erreur"] = text[: match.start()].strip()
            data["correction"] = text[match.end() :].strip()
            break

    # Si pas de split, detecter des regles directement
    if not data["correction"]:
        if re.search(r"(ne\s+jamais|toujours|il\s+faut)", text, re.IGNORECASE):
            data["regle_extraite"] = text
            data["correction"] = text

    return data


# ---------------------------------------------------------------------------
# LLM Fallback (DeepSeek via LLMProvider)
# ---------------------------------------------------------------------------

_LLM_PROMPTS = {
    "decision": (
        "Extrais les champs suivants de ce texte de d\u00e9cision "
        "(r\u00e9ponse JSON uniquement, pas de markdown) :\n"
        '- choix: ce qui a \u00e9t\u00e9 d\u00e9cid\u00e9\n'
        '- justification: pourquoi ce choix (vide "" si absent)\n'
        '- contexte: la situation (vide "" si absent)\n'
        '- alternatives: tableau des options \u00e9cart\u00e9es ([] si absent)\n\n'
        'Texte : "{text}"\nJSON :'
    ),
    "doute": (
        "Extrais les champs suivants de ce texte de doute "
        "(r\u00e9ponse JSON uniquement, pas de markdown) :\n"
        '- question: la question ou l\'h\u00e9sitation\n'
        '- contexte: la situation (vide "" si absent)\n\n'
        'Texte : "{text}"\nJSON :'
    ),
    "lecon": (
        "Extrais les champs suivants de cette le\u00e7on "
        "(r\u00e9ponse JSON uniquement, pas de markdown) :\n"
        '- erreur: ce qui s\'est mal pass\u00e9 ou le pi\u00e8ge identifi\u00e9\n'
        '- correction: la bonne pratique adopt\u00e9e\n'
        '- regle_extraite: la r\u00e8gle formalis\u00e9e (vide "" si pas explicite)\n\n'
        'Texte : "{text}"\nJSON :'
    ),
}


async def extract_llm(text: str, tag: str, llm_provider) -> dict:
    """Extraction via LLM (DeepSeek fallback). ~0.001$ par appel."""
    prompt = _LLM_PROMPTS.get(tag, "")
    if not prompt:
        return {}

    try:
        response = await llm_provider.chat(
            messages=[{"role": "user", "content": prompt.format(text=text)}],
            temperature=0.1,
            max_tokens=500,
        )
        cleaned = response.strip()
        # Nettoyer markdown ```json...```
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)

        data = json.loads(cleaned)
        logger.info("LLM extraction OK pour tag=%s", tag)
        return data
    except Exception as e:
        logger.warning("LLM extraction echouee: %s", e)
        return {}


# ---------------------------------------------------------------------------
# Score de confiance
# ---------------------------------------------------------------------------

def compute_confidence(data: dict, tag: str, forced: bool = False) -> float:
    """Score 0.0-1.0. forced=True via commande /decision, /doute, /lecon."""
    score = 0.0

    if tag == "decision":
        if data.get("choix"):
            score += 0.50
        if data.get("justification"):
            score += 0.30
        if data.get("contexte"):
            score += 0.20
    elif tag == "doute":
        if data.get("question"):
            score += 0.70
        if data.get("contexte"):
            score += 0.30
    elif tag == "lecon":
        if data.get("erreur"):
            score += 0.40
        if data.get("correction"):
            score += 0.40
        if data.get("regle_extraite"):
            score += 0.20

    # Bonus commande explicite : l'utilisateur a force le type
    if forced:
        score = min(1.0, score + 0.30)

    return round(score, 2)


# ---------------------------------------------------------------------------
# Rendu template
# ---------------------------------------------------------------------------

def render_template(data: dict, tag: str) -> str:
    """Formate le texte enrichi pour Graphiti add_episode."""
    template = TEMPLATES.get(tag, "")
    if not template:
        return str(data)

    date_str = datetime.utcnow().strftime("%d/%m/%Y %H:%M")
    values = dict(data)
    values["date"] = date_str

    # Alternatives → texte
    alts = values.get("alternatives", [])
    if isinstance(alts, list):
        values["alternatives"] = ", ".join(alts) if alts else "\u2014"
    elif not alts:
        values["alternatives"] = "\u2014"

    # Source ref suffix
    ref = values.get("source_ref") or ""
    values["source_ref_suffix"] = f" \u2014 {ref}" if ref else ""

    # Valeurs vides → tiret
    skip_keys = {"date", "source_ref_suffix", "alternatives", "source_ref",
                 "valid_from", "schema_version", "remplace_decision_id",
                 "decision_id", "doute_id", "apprentissage_id",
                 "resolution"}
    for key in values:
        if key not in skip_keys and values[key] in ("", None):
            values[key] = "\u2014"

    try:
        return template.format(**values)
    except KeyError as e:
        logger.warning("Template key manquante: %s", e)
        return str(data)


# ---------------------------------------------------------------------------
# Reponse Telegram formatee
# ---------------------------------------------------------------------------

_TAG_RESPONSE = {
    "decision": ("\U0001f4cc", "D\u00e9cision m\u00e9moris\u00e9e"),
    "doute": ("\u2753", "Doute enregistr\u00e9"),
    "lecon": ("\U0001f4a1", "Le\u00e7on retenue"),
}


def format_reasoning_response(
    tag: str, data: dict, score: float, vocal: bool = False
) -> str:
    """Message Telegram apres ingestion d'une entite de raisonnement."""
    mic = "\U0001f3a4 " if vocal else ""
    emoji, label = _TAG_RESPONSE.get(tag, ("\U0001f4dd", "M\u00e9moris\u00e9"))

    # Extrait principal
    if tag == "decision":
        main_text = data.get("choix", "")
    elif tag == "doute":
        main_text = data.get("question", "")
    else:
        main_text = data.get("erreur", "")

    lines = [f'{mic}{emoji} {label} : "{main_text}"']

    # Details
    if tag == "decision" and data.get("justification"):
        lines.append(f"  \u2192 {data['justification']}")
    if tag == "lecon" and data.get("regle_extraite"):
        lines.append(f"  \U0001f4d6 {data['regle_extraite']}")

    lines.append(f"\n\u2705 Score : {score:.0%}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pipeline principal
# ---------------------------------------------------------------------------

async def format_reasoning(
    text: str,
    tag: str,
    llm_provider,
    forced: bool = False,
) -> tuple[str, dict, float]:
    """Pipeline : heuristique → LLM fallback → Pydantic → score → template.

    Args:
        text: Texte brut du message
        tag: Type (decision, doute, lecon)
        llm_provider: Instance LLMProvider pour le fallback
        forced: True si via commande explicite (/decision, /doute, /lecon)

    Returns:
        (texte_enrichi, pydantic_dict, score)
    """
    # 1. Extraction heuristique
    data = extract_heuristic(text, tag)
    score = compute_confidence(data, tag, forced=forced)

    # 2. LLM fallback si score < seuil
    if score < REVIEW_THRESHOLD and llm_provider:
        logger.info(
            "Score %.2f < seuil %.2f — LLM fallback tag=%s",
            score, REVIEW_THRESHOLD, tag,
        )
        llm_data = await extract_llm(text, tag, llm_provider)
        if llm_data:
            for key, value in llm_data.items():
                if value and not data.get(key):
                    data[key] = value
            score = compute_confidence(data, tag, forced=forced)

    # 3. Validation Pydantic (best-effort — ne bloque pas le flux)
    model_cls = {
        "decision": DecisionInput,
        "doute": DouteInput,
        "lecon": ApprentissageInput,
    }.get(tag)

    pydantic_dict = data.copy()

    if model_cls:
        try:
            instance = model_cls(**data)
            # mode="json" : datetime → ISO string, pour serialisation JSON review_payload
            pydantic_dict = instance.model_dump(mode="json")
        except Exception as e:
            logger.warning("Validation Pydantic echouee (tag=%s): %s", tag, e)
            score = min(score, 0.50)

    # 4. Rendu template
    enriched_text = render_template(pydantic_dict, tag)

    return enriched_text, pydantic_dict, score
