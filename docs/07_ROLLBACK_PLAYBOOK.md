# Rollback Playbook

Last updated: 2026-02-23

## Rollback triggers
- Sustained 5xx errors after deployment.
- Broken core chat flow (`/api/chat/message` failing or timing out).
- Critical policy/validator regression.
- Migration-related data access failures.

## 1. Immediate containment (0-5 min)
- Pause new rollout traffic (or shift traffic to previous stable pods).
- Announce incident and freeze config edits in admin.
- Capture diagnostics:
- `GET /admin/api/observability/status`
- `GET /admin/api/evaluation/summary?hours=1`
- latest errors from `logs/observability.log`

## 2. Application rollback (5-15 min)
- Redeploy previous known-good image/tag.
- Validate:
- `GET /health`
- `POST /api/chat/message` (smoke request)
- `GET /admin`
- Keep gateway auth/rate-limit settings unchanged unless they caused the incident.

## 3. Configuration rollback
- Import last known-good config snapshot via `/admin/api/config/import`.
- Re-verify:
- business profile
- enabled intents/services/tools
- prompt template and NLU policy
- quick actions and escalation settings

## 4. Schema/data rollback (if migration caused issue)
- Run rollback script: `python run_db_rollback.py`.
- Validate schema with migration verify script if available.
- Re-run minimal DB checks:
- `/admin/api/db/status`
- critical read/write smoke operations

## 5. RAG/index rollback
- If retrieval quality regressed after ingest/reindex:
- restore previous knowledge source list
- run reindex with known-good sources
- validate with `/admin/api/rag/query`

## 6. Recovery verification
- Confirm core chat paths:
- greeting
- FAQ
- menu/catalog
- order/booking safe flow
- escalation handoff
- Confirm observability + evaluation endpoints update normally:
- `/admin/api/observability/events`
- `/admin/api/evaluation/events`

## 7. Post-incident actions
- Document root cause, blast radius, and exact rollback point.
- Add regression test before next release.
- Update release checklist with new guardrail if a gap was found.
