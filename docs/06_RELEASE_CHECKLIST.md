# Release Checklist

Last updated: 2026-02-23

## 1. Pre-release validation
- Confirm environment variables are set for target environment (`.env` / secret manager).
- Run schema migration precheck: `python run_db_migration.py`.
- Run full tests: `python -m pytest -q`.
- Run syntax check: `python -m compileall services api core agents handlers`.
- Verify RAG index status from admin: `/admin/api/rag/status`.
- Verify gateway controls:
- `GET /admin/api/observability/status` shows expected auth/rate-limit settings.
- Verify evaluation telemetry:
- Send 2-3 test messages and confirm `/admin/api/evaluation/summary` updates.

## 2. Config and prompt freeze
- Export current config snapshot from admin (`/admin/api/config/export`).
- Store snapshot with release tag (for rollback).
- Confirm intended prompt template and NLU policy for tenant.
- Confirm enabled intents/services/tools match rollout scope.

## 3. Runtime readiness
- Confirm health endpoint: `GET /health`.
- Confirm DB status endpoint: `GET /admin/api/db/status`.
- Confirm observability log writable (size increases after test request).
- Confirm trace headers are present on API responses:
- `X-Trace-Id`
- `X-Response-Time-Ms`

## 4. Deployment
- Deploy API pods with immutable image tag.
- Apply migrations (if any) once.
- Warm-up checks:
- `GET /health`
- `GET /admin`
- `POST /api/chat/message` smoke test
- Monitor first 15 minutes:
- 5xx rate
- p95 response time
- validator replacement rate
- complex path rate drift

## 5. Post-release sign-off
- Confirm no abnormal spikes in:
- `/admin/api/observability/events`
- `/admin/api/evaluation/summary`
- Confirm admin setup/edit flows still save config correctly.
- Record release note with:
- code tag
- migration id
- config snapshot id
- known limitations (channel parity, ticketing E2E, OCR pipeline if still pending)
