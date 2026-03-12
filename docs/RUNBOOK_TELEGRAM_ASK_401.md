# RUNBOOK - Telegram `/ask` en erreur 401

Date: 2026-03-12

## Symptom

Telegram repond:

`Erreur: Tous les providers ont echoue. Derniere erreur: 401 Unauthorized ... api.anthropic.com/v1/chat/completions`

## Root cause observee

- Le provider `claude` etait appele avec un payload/endpoint OpenAI (`/chat/completions`).
- L'API Anthropic attend `/v1/messages` + headers specifiques.
- Une cle placeholder (`ANTHROPIC_API_KEY=sk-ant-xxx`) pouvait polluer le fallback et masquer l'erreur initiale du provider principal.

## Correctif code

Fichier: `bot/llm_provider.py`

- `claude` passe par un appel natif Anthropic:
  - `POST https://api.anthropic.com/v1/messages`
  - headers: `x-api-key`, `anthropic-version`
- Les cles placeholders sont ignorees dans la chaine de fallback.
- Si aucune cle valide n'est disponible:
  - message explicite `Aucun provider LLM configure correctement`.

## Verification rapide

1. `GET /health` doit rester `status=ok`.
2. Tester `POST /api/context?q=cnrs` (memoire).
3. Tester `/ask` sur Telegram:
   - ne doit plus retourner 401 Anthropic si la cle est placeholder.

## Checklist prod

- Verifier `LLM_PROVIDER` dans `.env` (preferer `deepseek` si operationnel).
- Verifier que `DEEPSEEK_API_KEY` est valide.
- Laisser `ANTHROPIC_API_KEY` vide si non utilise (pas de placeholder).
