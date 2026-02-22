"""
IntentDetector - Detection d'intention + tags metier (Phase 6A/6B + Phase 7)
=============================================================================
Detecte automatiquement l'intention (question/memo/correction/forget)
et la categorie metier (decision/doute/lecon/probleme/rappel/option/constat)
via regex. Zero appel LLM, zero latence, zero cout.

Phase 7 : ajout tags raisonnement (doute, lecon) avec precedence
sur rappel/option pour eviter les faux positifs.
"""

import re
import logging

logger = logging.getLogger("agea.intent")

# --- Phase 6A : Detection d'intention ---

INTENT_PATTERNS = {
    "question": [
        r"^(qu['\u2019]est.ce|quel|quels|quelle|quelles|combien|comment|pourquoi|o[uù]|quand|qui)\b",
        r"\?$",
        r"^(dis.moi|rappelle.moi|c['\u2019]est quoi|on a quoi sur)",
        r"^(r[eé]sume|r[eé]cap|r[eé]sum[eé])",
    ],
    "correction": [
        r"(finalement|en fait|correction|rectification|erreur)",
        r"(je corrige|je rectifie|c['\u2019]est pas|c['\u2019]est plut[oô]t)",
        r"(non en fait|au final|je me suis tromp[eé])",
        r"(il faut changer|remplacer par|au lieu de)",
    ],
    "forget": [
        r"^(oublie|supprime|efface|retire|annule)",
        r"(c['\u2019]est plus d['\u2019]actualit[eé]|c['\u2019]est annul[eé]|on annule)",
    ],
}

# --- Phase 6B : Tags metier ---

# Ordre = precedence de detection. decision/lecon/doute AVANT rappel/option
# pour eviter que "il faut toujours verifier" soit detecte comme rappel.
BUSINESS_TAGS = {
    "decision": [
        r"(on a d[eé]cid[eé]|d[eé]cision|on prend|on choisit|c['\u2019]est valid[eé]|on part sur|on retient)",
        r"(validation|valid[eé] par|approuv[eé])",
    ],
    "lecon": [
        r"(le[cç]on|on a appris|ne plus jamais|dor[eé]navant)",
        r"(pi[eè]ge|retenir que|la prochaine fois)",
        r"(ne jamais|toujours v[eé]rifier|r[eè]gle\b)",
        r"(erreur.+correction|erreur.+maintenant)",
    ],
    "doute": [
        r"(j['\u2019]h[eé]site|on h[eé]site entre|pas s[uû]r|incertain|je sais pas)",
        r"(je me demande|on devrait|faut.il|vaut.il mieux)",
        r"(dilemme|h[eé]sitation)",
    ],
    "probleme": [
        r"(probl[eè]me|souci|blocage|bloqu[eé]|[cç]a passe pas|[cç]a marche pas)",
        r"(attention|vigilance|risque|incident|panne|d[eé]faut)",
    ],
    "option": [
        r"(option|alternative|soit .+ soit|ou bien|[aà] voir)",
        r"(proposition|on envisage|[eé]ventuellement)",
    ],
    "rappel": [
        r"(rappel|[aà] faire|penser [aà]|ne pas oublier|il faut|faut que)",
        r"(commander|appeler|relancer|envoyer|pr[eé]parer|v[eé]rifier)",
        r"(avant vendredi|avant lundi|deadline|urgent|asap|cette semaine)",
    ],
    "constat": [
        r"(on a constat[eé]|on a vu|[eé]tat des lieux|situation|avancement)",
        r"(aujourd['\u2019]hui|ce matin|ce soir|sur place)",
    ],
}

# --- UX : Emojis par intention et tag ---

INTENT_EMOJI = {
    "memo": "\U0001f4dd",       # 📝
    "question": "\U0001f50d",   # 🔍
    "correction": "\u270f\ufe0f",  # ✏️
    "forget": "\U0001f5d1\ufe0f",  # 🗑️
}

TAG_EMOJI = {
    "decision": "\U0001f4cc",   # 📌
    "doute": "\u2753",          # ❓
    "lecon": "\U0001f4a1",      # 💡
    "option": "\U0001f504",     # 🔄
    "probleme": "\u26a0\ufe0f", # ⚠️
    "rappel": "\u23f0",         # ⏰
    "constat": "\U0001f4cb",    # 📋
}

TAG_LABEL = {
    "decision": "D\u00e9cision m\u00e9moris\u00e9e",
    "doute": "Doute enregistr\u00e9",
    "lecon": "Le\u00e7on retenue",
    "option": "Option not\u00e9e",
    "probleme": "Probl\u00e8me not\u00e9",
    "rappel": "Rappel not\u00e9",
    "constat": "Constat not\u00e9",
}


def detect_intent(text: str) -> str:
    """Detecte l'intention a partir du texte.

    Retourne: question | correction | forget | memo (defaut).
    Les commandes /xxx ne doivent PAS passer par cette fonction.
    """
    text_lower = text.lower().strip()

    for intent, patterns in INTENT_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, text_lower):
                logger.debug("Intent '%s' detecte pour: %s", intent, text[:60])
                return intent

    return "memo"


def detect_business_tag(text: str) -> str | None:
    """Detecte la categorie metier.

    Retourne: decision | lecon | doute | probleme | option | rappel | constat | None.
    L'ordre de detection suit la precedence definie dans BUSINESS_TAGS.
    """
    text_lower = text.lower()

    for tag, patterns in BUSINESS_TAGS.items():
        for pattern in patterns:
            if re.search(pattern, text_lower):
                logger.debug("Tag '%s' detecte pour: %s", tag, text[:60])
                return tag

    return None


def format_response(intent: str, tag: str | None, text: str, vocal: bool = False) -> str:
    """Construit le message de reponse Telegram selon intention + tag."""
    mic = "\U0001f3a4 " if vocal else ""  # 🎤

    if tag:
        emoji = TAG_EMOJI.get(tag, "\U0001f4dd")
        label = TAG_LABEL.get(tag, "M\u00e9moris\u00e9")
    else:
        emoji = INTENT_EMOJI.get(intent, "\U0001f4dd")
        label = {
            "memo": "M\u00e9moris\u00e9",
            "correction": "Correction enregistr\u00e9e",
            "forget": "Information marqu\u00e9e obsol\u00e8te",
        }.get(intent, "M\u00e9moris\u00e9")

    return f"{mic}{emoji} {label} : \"{text}\"\n\nStructuration en cours..."


def tag_content(text: str, tag: str | None) -> str:
    """Prefixe le contenu avec le tag metier pour Zep/Graphiti."""
    if tag:
        return f"[{tag.upper()}] {text}"
    return text
