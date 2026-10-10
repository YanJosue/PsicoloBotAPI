# Phase 0 — Safety & privacy hardening

Feature id: `phase0-safety-privacy` · Engram mirror: `odd/phase0-safety-privacy/tasks` (project `psicolobot`)

## Objective
Make the PsicoloBot prototype safe to put in front of a real person: crisis detection that does not depend on the LLM, honest privacy communication, explicit consent with an 18+ gate, and an API that isolates and protects each user's conversation.

## Problem / why
An audit (2026-09-29) verified that:
- The regex crisis guardrail misses common phrasings ("quiero suicidarme", "me quiero quitar la vida", "me voy a colgar", "pienso en el suicidio"), false-positives figurative ones ("me quiero morir de risa", "me corto el pelo"), is skipped when the model JSON fails to parse, and forgets risk between turns.
- The app claims "funciona solo en tu dispositivo", shows a "Local" pill and says the check-in "no se guarda"; in reality text travels through ngrok to a server that keeps it in plaintext SQLite indefinitely.
- No consent, privacy notice or age gate. The API has no auth, a shared `default_session`, `allow_origins=["*"]`, binds `0.0.0.0`, and leaks raw model output (`error_debug`).
- An unknown risk label crashes `RiskBadge` on every launch; network failures fabricate a high-risk analysis.

## Scope (authorized 2026-10-09)
- API: `Tesis/psicolobot-scraper/API/PsicoloBotAPI` (GitHub `YanJosue/PsicoloBotAPI`, base `main` @ d85a022).
- App: `Tesis/psicolobot-mobile` (Expo 54; not a git repo yet → `git init` + baseline snapshot commit).
- Out of scope: model retraining, evaluation fixes, EI features, article edits, the stale API copies (`Psicolobot_API_Deploy/`, root `psicolobot_api.py` in the scraper repo).

## Constraints
- Strict TDD (source: user global CLAUDE.md "Strict TDD Mode: enabled"). Observe RED before GREEN.
  - API runner: `python3 -m pytest` (pytest 9.0.2, /opt/anaconda3). Tests must not need a GPU, torch or unsloth.
  - App runner: none exists → task M1 adds `jest-expo` (`npm test`). Until then only `npm run typecheck`.
- Artifacts (code, comments, docs) in English; user-facing UI copy in neutral Mexican Spanish (tú, with accents).
- Crisis resources (verified against official sources): Línea de la Vida 800 911 2000 (24/7), SAPTEL 55 5259 8121 (24/7), Emergencias 911.
- Recall-first crisis detection: false positives are acceptable, false negatives are not. Never log user text.
- Do not drop the legacy `memory` table automatically; document that it should be deleted.

## API contract (target)
- `POST /sesion` body `{acepta_terminos: true, mayor_de_edad: true, version_consentimiento: str}` → `201 {token, expira_en_dias}`; 422 unless both flags are true. Token: opaque random, stored only as SHA-256.
- `POST /analizar` header `Authorization: Bearer <token>`, body `{texto}` → 401 if token missing/invalid/expired. Response: `{emocion, riesgo, riesgo_sesion, tematica, resumen, respuesta, modismos, guardrail_activado, recursos_crisis, procesado}`; no `error_debug`.
- `DELETE /sesion` (Bearer) → 204, deletes the session and its messages.
- Optional `X-API-Key` when `PSICOLOBOT_API_KEY` is set (not a secret on mobile; only a scanner deterrent).
- Env: `PSICOLOBOT_DB_PATH`, `PSICOLOBOT_RETENTION_DAYS` (30), `PSICOLOBOT_CORS_ORIGINS` (empty), `PSICOLOBOT_HOST` (127.0.0.1), `PSICOLOBOT_PORT` (8000), `PSICOLOBOT_API_KEY` (unset).

## Tasks
Route legend: D = delegated direct (trigger: 2+ non-trivial files incl. tests).

### API (`PsicoloBotAPI`, branch `feat/phase0-safety-privacy`)
- [x] **A1** Testable structure: lazy model loading behind an injectable generator, env-configurable DB path, pytest harness with a fake generator. Route D.
- [ ] **A2** Independent crisis layer (`safety.py`): accent/orthography normalization, expanded Mexican-Spanish CRITICO/ALTO patterns, figurative exclusions, applied to the full raw text before the LLM and on every path (success, parse failure, exception); final risk = max(model, safety); crisis resources payload; safe reply template when the model fails at ≥ ALTO. Route D.
- [ ] **A3** Output validation: Pydantic response model, risk/emotion normalization (unknown risk never lowers safety), robust JSON extraction, `procesado` flag, remove `error_debug`. Route D.
- [ ] **A4** Sessions & privacy: consent-gated token sessions, Bearer auth, `DELETE /sesion`, per-session max risk with 24 h window (`riesgo_sesion`, injected into the prompt), retention purge, CORS/host from env, optional API key. Route D.
- [ ] **A5** Docs: README/INSTRUCCIONES for env vars, endpoints, legacy DB cleanup, safety-layer limits. Route D.

### App (`psicolobot-mobile`, branch `feat/phase0-safety-privacy` after baseline commit on `main`)
- [ ] **M0** `git init -b main`, verify `.gitignore`, baseline snapshot commit (pre-existing code, not authored here). Route inline (mechanical).
- [ ] **M1** Test runner: `jest-expo` + `@testing-library/react-native`, `npm test`, smoke test. Route D.
- [ ] **M2** Honest copy & config: remove "Local" pill and false claims, accurate data notice, API URL/key from `EXPO_PUBLIC_*` env. Route D.
- [ ] **M3** Consent onboarding: first-run gate (what it is/isn't, data handling, 18+, terms), `POST /sesion`, persisted consent version + token; chat blocked until accepted. Route D.
- [ ] **M4** API client hardening: Bearer token, timeout, response validation, typed errors instead of fabricated high risk, 401 → re-consent, "Borrar" calls `DELETE /sesion`. Route D.
- [ ] **M5** Risk UI robustness: `RiskBadge` fallback, `CrisisBanner` on `riesgo`/`riesgo_sesion` ≥ ALTO with tap-to-call resources (server payload, local fallback). Route D.

## Acceptance criteria
- All listed crisis phrasings → CRITICO; figurative phrasings do not escalate to CRITICO; detection works with and without accents; a parse failure or model exception still escalates and returns resources.
- No API path accepts a request without a valid session token; sessions are isolated; deleting a session removes its messages.
- The app never claims on-device processing; chat is unreachable until consent + 18+ are accepted.
- Unknown risk labels never crash the app; network errors never fabricate a risk level.
- API: `python3 -m pytest` green. App: `npm test` and `npm run typecheck` green.

## Delivery
- Forecast: ~1,150 authored lines API + ~800 app (lockfiles excluded) → above the ~400-line budget; strategy `ask-on-risk`, chain strategy chosen by the user 2026-10-09: `stacked-to-main` (each slice PR targets main, stacked on the previous one; main is temporarily incompatible between the API auth slice and the app slices — expected).
- Slice boundaries (API): S1 = `c21baff` (A1).
- Work-unit commits on `feat/phase0-safety-privacy` in each repo; push/PR are the user's decision.
- RDD is on (global): after each work-unit commit run `gentle-ai review assess --committed-only` from the last reviewed boundary (first boundary = branch point).

## Progress & evidence
| Task | Commit | Checks | RDD assess | Notes |
|---|---|---|---|---|
| A1 | `c21baff` | RED: 5 failed/17 errors (`ModuleNotFoundError: unsloth`, `llm`); GREEN: `python3 -m pytest -q` 22 passed (writer + parent spot check) | high (`process_boundary` in tests) → consent granted → 4-lens review approved, acknowledged (lineage `review-edf40badd97734a0`, authority burned). Reviewed boundary → `c21baff` | Start script now `python psicolobot_api.py` so the model preloads; model path absolute; local `llm.py` shadows the PyPI `llm` package name. |

## Next step
A2.
