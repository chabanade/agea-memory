"""
reasoning_models — Modeles Pydantic pour la memoire de raisonnement (Phase 7)
==============================================================================
3 entites : Decision, Doute, Apprentissage.
Validation structurelle AVANT ingestion dans Graphiti.
Zero LLM — validation pure.
"""

from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

# Cles ASCII sans accents (recommandation Codex — evite encodage DB/API)
SourceType = Literal["certified", "verified", "unverified", "expertise", "ia", "obsolete"]


class DecisionInput(BaseModel):
    """Decision prise par Mehdi — choix + justification + source."""

    schema_version: str = "1.0"
    decision_id: str = Field(default_factory=lambda: str(uuid4()))
    choix: str                                     # OBLIGATOIRE
    contexte: str = ""
    justification: str = ""
    alternatives: list[str] = []
    source_type: SourceType = "unverified"
    source_ref: str | None = None                  # "terrain CNVL 22/02", URL, doc_id
    valid_from: datetime = Field(default_factory=datetime.utcnow)
    statut: Literal["active", "replaced", "revoked"] = "active"
    remplace_decision_id: str | None = None        # Chainage versions

    @model_validator(mode="after")
    def expertise_needs_ref(self):
        """source_type='expertise' requiert source_ref pour tracabilite."""
        if self.source_type == "expertise" and not self.source_ref:
            raise ValueError(
                "source_type='expertise' requiert source_ref "
                "(ex: 'terrain CNVL 22/02', 'visite chantier')"
            )
        return self


class DouteInput(BaseModel):
    """Doute ou hesitation — question ouverte a resoudre."""

    schema_version: str = "1.0"
    doute_id: str = Field(default_factory=lambda: str(uuid4()))
    question: str                                  # OBLIGATOIRE
    contexte: str = ""
    statut: Literal["ouvert", "resolu", "invalide"] = "ouvert"
    resolution: str | None = None
    source_type: SourceType = "unverified"
    valid_from: datetime = Field(default_factory=datetime.utcnow)


class ApprentissageInput(BaseModel):
    """Lecon apprise — erreur + correction + regle extraite."""

    schema_version: str = "1.0"
    apprentissage_id: str = Field(default_factory=lambda: str(uuid4()))
    erreur: str                                    # OBLIGATOIRE
    correction: str                                # OBLIGATOIRE
    regle_extraite: str = ""
    contexte: str = ""
    source_type: SourceType = "verified"
    source_ref: str | None = None
    valid_from: datetime = Field(default_factory=datetime.utcnow)
