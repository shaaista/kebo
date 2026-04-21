# Logging Plan (1-Week Retention)

## Objectives
- Capture request lifecycle logs for both admin and chat paths.
- Capture deep step-level logs for chat turn processing and form submission.
- Keep logs for exactly 7 days, then auto-delete by file age.
- Keep behavior safe: logging-only changes, no business logic changes.

## Primary Log Files and Purpose
- `logs/step_trace.jsonl`: request/route/step execution trail (gateway + admin routes + chat routes + chat deep steps).
- `logs/llm_call_metrics.jsonl`: canonical per-call LLM accounting (duration, token usage, cost, session/trace).
- `logs/chat_turn_llm_summary.jsonl`: one row per chat turn with user message + total LLM call count + per-call duration/tokens/cost.
- `logs/chat_step_timing.jsonl`: one row per chat message with end-to-end timing breakdown by processing step plus LLM-call totals.
- `logs/backend_trace.jsonl`: structured backend trace events (sanitized).
- `logs/everything_backend_trace.jsonl`: full-fidelity backend trace for deep debugging.
- `logs/turn_diagnostics.jsonl`: per-chat-turn diagnostics and turn context.
- `logs/conversation_audit.jsonl`: chat request/response audit trail.
- `logs/observability.log`: operational API events and gateway events.
- `logs/flow.log`: human-readable end-to-end flow log.
- `logs/kb_pipeline.log`: KB upload/index/pull pipeline events.
- `logs/service_config.log`: service config save + prompt generation snapshots.
- `logs/service_runtime.log`: runtime service-agent decisions.
- `logs/processing_debug.jsonl`: compact processing events from flow logger.
- `logs/ticketing_debug.jsonl`: ticketing create/update debug events.
- `logs/detailedsteps.log`: RAG step logs.
- `logs/gateway_crash.log`: unhandled gateway exceptions with traceback.
- `logs/pull_from_kb_debug.txt`: KB extraction debug helper log.

## Duplicate vs Layered Logs
- Not duplicates (intentional layers):
  - `llm_call_metrics.jsonl` = billing/latency/token accounting and session traceability for every LLM call.
  - `observability.log` = ops summary.
  - `backend_trace.jsonl` = structured backend details.
  - `everything_backend_trace.jsonl` = verbose deep payloads.
  - `step_trace.jsonl` = stage-by-stage request lifecycle markers.
- Real duplicates to avoid:
  - `llm_inputs.log` (redundant with `llm_io_trace.jsonl` + `everything_backend_trace.jsonl`) is disabled by default.
  - Multiple ad-hoc uvicorn run logs (`uvicorn_*.out.log`, `uvicorn_*.err.log`) should stay ephemeral under 7-day cleanup.

## Retention Policy
- Retention window: `7 days`.
- Startup cleanup: enabled.
- Periodic cleanup: every `360` minutes.
- Scope: all files under `logs/**` plus root `tmp_uvicorn_*.log`.

## Missing-Log Prevention
- On startup:
  - `services.flow_logger.ensure_log_files()` ensures flow log files exist.
  - `services.log_setup_service.ensure_configured_log_files()` ensures every `*_log_file` from settings exists.
- This prevents “file missing” confusion during incident debugging.

## Incident Debugging Path
- Start with `logs/step_trace.jsonl` for lifecycle path and failure stage.
- Correlate with `trace_id` / `turn_trace_id`.
- Use `backend_trace.jsonl` for structured payload context.
- Use `everything_backend_trace.jsonl` only when payload-level detail is required.
- Validate chat decision chain in `turn_diagnostics.jsonl` and `flow.log`.
