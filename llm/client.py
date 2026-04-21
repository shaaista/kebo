"""
LLM Client - OpenAI Integration

Handles all LLM calls with proper error handling and logging.
"""

import json
import inspect
import logging
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Optional
from openai import AsyncOpenAI

from config.settings import settings
from services.config_service import config_service
from services.faq_context_service import build_faq_block
from services.llm_simple_call_logger import llm_simple_call_logger
from services.turn_diagnostics_service import turn_diagnostics_service


class LLMClient:
    """Async OpenAI client wrapper."""

    def __init__(self):
        self.client = AsyncOpenAI(api_key=settings.openai_api_key)
        self.model = settings.openai_model
        self.max_tokens = settings.llm_max_tokens
        self.temperature = settings.llm_temperature
        self.llm_trace_enabled = bool(getattr(settings, "llm_io_trace_enabled", True))
        self.llm_trace_file = Path(str(getattr(settings, "llm_io_trace_file", "./logs/llm_io_trace.jsonl")))
        self.llm_trace_max_chars = max(2000, int(getattr(settings, "llm_io_trace_max_chars", 300000) or 300000))
        self._llm_trace_logger = logging.getLogger("llm_io_trace")
        if not self._llm_trace_logger.handlers:
            self.llm_trace_file.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(self.llm_trace_file, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._llm_trace_logger.addHandler(handler)
            self._llm_trace_logger.setLevel(logging.INFO)
            self._llm_trace_logger.propagate = False

    @staticmethod
    def _render_admin_prompt(template: str, context: dict) -> str:
        """Render simple placeholders from admin-authored prompt text."""
        if not template:
            return ""

        rendered = template
        replacements = {
            "{bot_name}": str(context.get("bot_name", "Assistant")),
            "{business_name}": str(context.get("hotel_name", context.get("hotel_code", "Business"))),
            "{hotel_name}": str(context.get("hotel_name", context.get("hotel_code", "Business"))),
            "{city}": str(context.get("city", "")),
            "{business_type}": str(context.get("business_type", "generic")),
        }
        for token, value in replacements.items():
            rendered = rendered.replace(token, value)
        return rendered

    @staticmethod
    def _list_or_fallback(items: list, fallback: str) -> str:
        cleaned = [str(item).strip() for item in items if str(item).strip()]
        if not cleaned:
            return f"- {fallback}"
        return "\n".join(f"- {item}" for item in cleaned)

    @staticmethod
    def _history_limits() -> tuple[int, int]:
        """
        Return (max_messages, max_chars) for prompt history rendering.
        max_messages <= 0 means use all provided history messages.
        """
        max_messages = int(getattr(settings, "llm_history_max_messages", 0) or 0)
        max_chars = max(800, int(getattr(settings, "llm_history_max_chars", 12000) or 12000))
        return max_messages, max_chars

    @classmethod
    def _build_history_string(cls, conversation_history: list[dict]) -> str:
        max_messages, max_chars = cls._history_limits()
        items = list(conversation_history or [])
        if max_messages > 0:
            items = items[-max_messages:]

        lines: list[str] = []
        for msg in items:
            role = str(msg.get("role") or "").strip().upper() or "UNKNOWN"
            content = str(msg.get("content") or "").strip()
            if not content:
                continue
            lines.append(f"{role}: {content}")

        history = "\n".join(lines).strip()
        if not history:
            return "No previous messages"
        if len(history) <= max_chars:
            return history
        return history[-max_chars:]

    @staticmethod
    def _coerce_json_safe(value: Any, depth: int = 0) -> Any:
        if depth > 8:
            return str(value)
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, dict):
            return {
                str(k): LLMClient._coerce_json_safe(v, depth + 1)
                for k, v in list(value.items())[:200]
            }
        if isinstance(value, list):
            return [LLMClient._coerce_json_safe(item, depth + 1) for item in value[:200]]
        if isinstance(value, tuple):
            return [LLMClient._coerce_json_safe(item, depth + 1) for item in value[:200]]
        return str(value)

    @classmethod
    def _message_content_to_text(cls, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())
                        continue
                    item_text = item.get("input_text")
                    if isinstance(item_text, str) and item_text.strip():
                        parts.append(item_text.strip())
                        continue
                    parts.append(json.dumps(cls._coerce_json_safe(item), ensure_ascii=False))
                    continue
                snippet = str(item or "").strip()
                if snippet:
                    parts.append(snippet)
            return "\n".join(parts).strip()
        if isinstance(content, dict):
            text = content.get("text")
            if isinstance(text, str):
                return text
            return json.dumps(cls._coerce_json_safe(content), ensure_ascii=False)
        if content is None:
            return ""
        return str(content)

    @staticmethod
    def _try_parse_json_object(text: str, *, max_chars: int = 1_200_000) -> dict[str, Any]:
        stripped = str(text or "").strip()
        if not stripped:
            return {}
        if len(stripped) > max_chars:
            return {}
        if not (stripped.startswith("{") and stripped.endswith("}")):
            return {}
        try:
            parsed = json.loads(stripped)
        except Exception:
            return {}
        if isinstance(parsed, dict):
            return parsed
        return {}

    @classmethod
    def _extract_user_query(cls, messages: list[dict]) -> str:
        for message in reversed(list(messages or [])):
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "").strip().lower()
            if role != "user":
                continue
            text = cls._message_content_to_text(message.get("content")).strip()
            if not text:
                continue
            candidate = text
            payload = cls._try_parse_json_object(text, max_chars=1_200_000)
            if payload:
                for key in ("user_message", "query", "message", "input"):
                    value = payload.get(key)
                    if isinstance(value, str) and value.strip():
                        candidate = value.strip()
                        break
            return candidate
        return ""

    @classmethod
    def _extract_message_inputs(cls, messages: list[dict]) -> dict[str, Any]:
        system_chunks: list[str] = []
        last_user_message = ""
        for message in list(messages or []):
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "").strip().lower()
            content_text = cls._message_content_to_text(message.get("content")).strip()
            if not content_text:
                continue
            if role == "system":
                system_chunks.append(content_text)
            elif role == "user":
                last_user_message = content_text

        parsed_payload = cls._try_parse_json_object(last_user_message, max_chars=1_200_000)
        context_like_inputs: dict[str, Any] = {}
        if parsed_payload:
            for key, value in parsed_payload.items():
                key_str = str(key or "")
                key_lower = key_str.strip().lower()
                if not key_lower:
                    continue
                if any(
                    token in key_lower
                    for token in (
                        "state",
                        "history",
                        "context",
                        "memory",
                        "phase",
                        "service",
                        "knowledge",
                        "kb",
                        "policy",
                        "pending",
                        "ui",
                        "tool",
                    )
                ):
                    context_like_inputs[key_str] = value

        return {
            "system_prompt": "\n\n".join(system_chunks).strip(),
            "last_user_message": last_user_message,
            "parsed_user_payload": parsed_payload or {},
            "parsed_payload_keys": sorted(str(key) for key in parsed_payload.keys()) if parsed_payload else [],
            "context_like_inputs": context_like_inputs,
            "messages": messages,
        }

    @classmethod
    def _messages_contain_json_keyword(cls, messages: list[dict]) -> bool:
        for message in list(messages or []):
            if not isinstance(message, dict):
                continue
            content_text = cls._message_content_to_text(message.get("content")).strip().lower()
            if not content_text:
                continue
            if "json" in content_text:
                return True
        return False

    @classmethod
    def _ensure_json_keyword_for_json_mode(cls, messages: list[dict]) -> list[dict]:
        normalized_messages = [msg for msg in list(messages or []) if isinstance(msg, dict)]
        if cls._messages_contain_json_keyword(normalized_messages):
            return normalized_messages
        shim = {
            "role": "system",
            "content": "Return valid JSON only. Output must be a JSON object.",
        }
        return [shim, *normalized_messages]

    @staticmethod
    def _caller_context() -> dict[str, Any]:
        default = {
            "caller_module": "",
            "caller_function": "",
            "caller_file": "",
            "responder_type": "other",
            "service_id": "",
            "service_name": "",
        }
        stack = []
        try:
            stack = inspect.stack()
            for frame_info in stack[2:]:
                filename = str(frame_info.filename or "").replace("\\", "/")
                if filename.endswith("/llm/client.py"):
                    continue
                module = str(frame_info.frame.f_globals.get("__name__", "") or "")
                function = str(frame_info.function or "")
                context = dict(default)
                context["caller_module"] = module
                context["caller_function"] = function
                context["caller_file"] = filename

                is_orchestrator = (
                    filename.endswith("/services/llm_orchestration_service.py")
                    or module.endswith("services.llm_orchestration_service")
                )
                if is_orchestrator:
                    if function == "_run_service_agent":
                        context["responder_type"] = "service"
                        local_service_id = frame_info.frame.f_locals.get("service_id")
                        local_service_name = frame_info.frame.f_locals.get("service_name")
                        if local_service_id is not None:
                            context["service_id"] = str(local_service_id)
                        if local_service_name is not None:
                            context["service_name"] = str(local_service_name)
                    elif function == "orchestrate_turn":
                        context["responder_type"] = "main"
                    else:
                        context["responder_type"] = "main"
                    return context

                if module.endswith("services.config_service"):
                    if "enrich_service_kb_records" in function:
                        context["responder_type"] = "service_enrichment"
                    else:
                        context["responder_type"] = "config"
                    return context

                if module.endswith("services.full_kb_llm_service"):
                    context["responder_type"] = "full_kb_main"
                    return context

                if module.endswith("services.chat_service"):
                    context["responder_type"] = "chat_runtime"
                    return context

                return context
        except Exception:
            return default
        finally:
            del stack
        return default

    @staticmethod
    def _resolve_trace_actor(
        caller: dict[str, Any],
        trace_context: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        context = trace_context if isinstance(trace_context, dict) else {}
        caller_type = str(caller.get("responder_type") or "").strip().lower()
        requested_type = str(context.get("responder_type") or "").strip().lower()
        base_type = requested_type or caller_type or "other"
        scope_map = {
            "main": "main",
            "service": "service",
            "service_enrichment": "service",
            "full_kb_main": "main",
            "chat_runtime": "main",
            "config": "other",
            "other": "other",
        }
        answered_by = scope_map.get(base_type, "other")
        service_id = str(context.get("service_id") or caller.get("service_id") or "").strip()
        service_name = str(context.get("service_name") or caller.get("service_name") or "").strip()
        agent_name = str(
            context.get("agent")
            or context.get("agent_name")
            or caller.get("caller_function")
            or ""
        ).strip()
        return {
            "answered_by": answered_by,
            "responder_type": base_type,
            "agent": agent_name,
            "service_id": service_id,
            "service_name": service_name,
            "session_id": str(context.get("session_id") or "").strip(),
        }

    @staticmethod
    def _truncate_large_strings(value: Any, max_chars: int, depth: int = 0) -> Any:
        if depth > 12:
            return str(value)
        if isinstance(value, str):
            if len(value) <= max_chars:
                return value
            omitted = len(value) - max_chars
            return f"{value[:max_chars]}\n...[TRUNCATED {omitted} chars]"
        if isinstance(value, dict):
            return {
                str(key): LLMClient._truncate_large_strings(item, max_chars=max_chars, depth=depth + 1)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [LLMClient._truncate_large_strings(item, max_chars=max_chars, depth=depth + 1) for item in value]
        if isinstance(value, tuple):
            return [LLMClient._truncate_large_strings(item, max_chars=max_chars, depth=depth + 1) for item in value]
        return value

    @staticmethod
    def _response_usage(response: Any) -> dict[str, Any]:
        usage = getattr(response, "usage", None)
        if usage is None:
            return {}
        result: dict[str, Any] = {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = getattr(usage, key, None)
            if value is not None:
                result[key] = int(value)
        return result

    @staticmethod
    def _usage_token_counts(usage: dict[str, Any]) -> tuple[int, int, int]:
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or 0)
        if total_tokens <= 0:
            total_tokens = max(0, prompt_tokens + completion_tokens)
        return prompt_tokens, completion_tokens, total_tokens

    @staticmethod
    def _derive_simple_call_purpose(
        *,
        operation: str,
        trace_context: dict[str, Any],
        caller: dict[str, Any],
        default_purpose: str,
    ) -> str:
        explicit_purpose = str(
            trace_context.get("what_this_call_is_for")
            or trace_context.get("purpose")
            or trace_context.get("description")
            or ""
        ).strip()
        if explicit_purpose:
            return explicit_purpose

        context_tag = str(
            trace_context.get("component")
            or trace_context.get("agent")
            or trace_context.get("actor")
            or trace_context.get("service_name")
            or trace_context.get("service_id")
            or ""
        ).strip()
        if context_tag:
            readable = context_tag.replace("_", " ").replace("-", " ").strip()
            if operation == "embedding":
                return f"Create embeddings for {readable}"
            if operation == "chat_with_json":
                return f"Generate JSON output for {readable}"
            return f"Generate assistant output for {readable}"

        caller_function = str(caller.get("caller_function") or "").strip()
        if caller_function:
            readable_fn = caller_function.replace("_", " ").strip()
            if operation == "embedding":
                return f"Create embeddings for {readable_fn}"
            if operation == "chat_with_json":
                return f"Generate JSON output for {readable_fn}"
            return f"Generate assistant output for {readable_fn}"

        return default_purpose

    def _log_llm_simple_call(
        self,
        *,
        call_id: str,
        operation: str,
        model: str,
        duration_ms: float,
        status: str,
        trace_context: Optional[dict[str, Any]] = None,
        caller: Optional[dict[str, Any]] = None,
        usage: Optional[dict[str, Any]] = None,
        purpose: str = "",
        input_preview: Any = "",
        output_preview: Any = "",
        error: str = "",
    ) -> dict[str, Any]:
        trace_meta = trace_context if isinstance(trace_context, dict) else {}
        caller_meta = caller if isinstance(caller, dict) else {}
        usage_map = usage if isinstance(usage, dict) else {}
        turn_ctx = turn_diagnostics_service.get_turn_context()
        turn_ctx = turn_ctx if isinstance(turn_ctx, dict) else {}
        input_tokens, output_tokens, total_tokens = self._usage_token_counts(usage_map)

        default_purpose = {
            "chat": "Generate assistant response",
            "chat_with_json": "Generate structured JSON response",
            "chat_completion": "Generate assistant completion",
            "embedding": "Create embedding vector",
        }.get(operation, "LLM call")
        final_purpose = str(purpose or "").strip() or self._derive_simple_call_purpose(
            operation=operation,
            trace_context=trace_meta,
            caller=caller_meta,
            default_purpose=default_purpose,
        )

        route = str(
            trace_meta.get("route")
            or trace_meta.get("path")
            or trace_meta.get("endpoint")
            or turn_ctx.get("route")
            or ""
        ).strip()
        component = str(
            trace_meta.get("component")
            or trace_meta.get("agent")
            or trace_meta.get("actor")
            or trace_meta.get("service_name")
            or turn_ctx.get("component")
            or ""
        ).strip()
        session_id = str(
            trace_meta.get("session_id")
            or trace_meta.get("session")
            or turn_ctx.get("session_id")
            or ""
        ).strip()
        trace_id = str(
            trace_meta.get("trace_id")
            or trace_meta.get("api_trace_id")
            or turn_ctx.get("api_trace_id")
            or ""
        ).strip()
        turn_trace_id = str(
            trace_meta.get("turn_trace_id")
            or turn_ctx.get("turn_trace_id")
            or ""
        ).strip()
        hotel_code = str(
            trace_meta.get("hotel_code")
            or trace_meta.get("tenant_id")
            or turn_ctx.get("hotel_code")
            or ""
        ).strip()
        channel = str(
            trace_meta.get("channel")
            or turn_ctx.get("channel")
            or ""
        ).strip()
        if session_id:
            session_key = session_id
        elif trace_id:
            session_key = f"trace:{trace_id}"
        elif turn_trace_id:
            session_key = f"turn:{turn_trace_id}"
        else:
            session_key = "global"
        caller_module = str(caller_meta.get("caller_module") or "").strip()
        caller_function = str(caller_meta.get("caller_function") or "").strip()

        try:
            input_text = (
                input_preview
                if isinstance(input_preview, str)
                else json.dumps(self._coerce_json_safe(input_preview), ensure_ascii=False)
            )
        except Exception:
            input_text = str(input_preview or "")
        try:
            output_text = (
                output_preview
                if isinstance(output_preview, str)
                else json.dumps(self._coerce_json_safe(output_preview), ensure_ascii=False)
            )
        except Exception:
            output_text = str(output_preview or "")

        metric_record = llm_simple_call_logger.log_call(
            call_id=call_id,
            operation=operation,
            model=model,
            purpose=final_purpose,
            duration_ms=duration_ms,
            status=status,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            session_id=session_id,
            session_key=session_key,
            hotel_code=hotel_code,
            channel=channel,
            trace_id=trace_id,
            turn_trace_id=turn_trace_id,
            route=route,
            component=component,
            caller_module=caller_module,
            caller_function=caller_function,
            input_preview=input_text,
            output_preview=output_text,
            error=str(error or ""),
        )
        try:
            if isinstance(metric_record, dict):
                turn_diagnostics_service.record_llm_call_metrics(metric_record)
        except Exception:
            pass
        return metric_record if isinstance(metric_record, dict) else {}

    def _log_llm_trace(self, payload: dict[str, Any]) -> None:
        try:
            safe_payload = self._coerce_json_safe(payload)
            safe_payload = self._truncate_large_strings(safe_payload, max_chars=self.llm_trace_max_chars)
            if self.llm_trace_enabled:
                self._llm_trace_logger.info(
                    json.dumps(safe_payload, ensure_ascii=False)
                )
            turn_diagnostics_service.log_llm_trace(safe_payload if isinstance(safe_payload, dict) else payload)
        except Exception:
            return

    async def chat(
        self,
        messages: list[dict],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        trace_context: Optional[dict[str, Any]] = None,
    ) -> str:
        """
        Send chat completion request to OpenAI.

        Args:
            messages: List of message dicts with 'role' and 'content'
            model: Override default model
            temperature: Override default temperature
            max_tokens: Override default max tokens

        Returns:
            Assistant's response text
        """
        started = time.perf_counter()
        request_id = uuid.uuid4().hex
        caller = self._caller_context()
        trace_meta = trace_context if isinstance(trace_context, dict) else {}
        trace_actor = self._resolve_trace_actor(caller, trace_meta)
        input_snapshot = self._extract_message_inputs(messages)
        request_model = model or self.model
        request_temperature = temperature if temperature is not None else self.temperature
        request_max_tokens = max_tokens or self.max_tokens
        user_query = self._extract_user_query(messages)
        try:
            response = await self.client.chat.completions.create(
                model=request_model,
                messages=messages,
                temperature=request_temperature,
                max_tokens=request_max_tokens,
            )
            output_text = response.choices[0].message.content or ""
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            usage = self._response_usage(response)
            self._log_llm_simple_call(
                call_id=request_id,
                operation="chat",
                model=request_model,
                duration_ms=duration_ms,
                status="success",
                trace_context=trace_meta,
                caller=caller,
                usage=usage,
                input_preview={"messages": messages, "user_query": user_query},
                output_preview=output_text,
            )
            self._log_llm_trace(
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "request_id": request_id,
                    "method": "chat",
                    "status": "success",
                    "duration_ms": duration_ms,
                    "model": request_model,
                    "temperature": request_temperature,
                    "max_tokens": request_max_tokens,
                    "user_query": user_query,
                    "answered_by": trace_actor.get("answered_by"),
                    "service_llm": {
                        "id": trace_actor.get("service_id"),
                        "name": trace_actor.get("service_name"),
                    },
                    "inputs": input_snapshot,
                    "output": output_text,
                    "usage": usage,
                    "caller": caller,
                    "trace_actor": trace_actor,
                    "trace_context": trace_meta,
                }
            )
            try:
                from services.flow_logger import log_llm_call
                log_llm_call(
                    actor=str(trace_actor.get("answered_by") or trace_actor.get("actor") or caller.get("function") or "llm"),
                    messages=messages,
                    response=output_text,
                    model=request_model,
                    temperature=request_temperature,
                    max_tokens=request_max_tokens,
                    duration_ms=duration_ms,
                    status="success",
                    trace_context=trace_meta,
                )
            except Exception:
                pass
            return output_text
        except Exception as e:
            try:
                print(f"LLM Chat Error: {e}")
            except OSError:
                pass
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            self._log_llm_simple_call(
                call_id=request_id,
                operation="chat",
                model=request_model,
                duration_ms=duration_ms,
                status="error",
                trace_context=trace_meta,
                caller=caller,
                usage={},
                input_preview={"messages": messages, "user_query": user_query},
                output_preview="",
                error=str(e),
            )
            self._log_llm_trace(
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "request_id": request_id,
                    "method": "chat",
                    "status": "error",
                    "duration_ms": duration_ms,
                    "model": request_model,
                    "temperature": request_temperature,
                    "max_tokens": request_max_tokens,
                    "user_query": user_query,
                    "answered_by": trace_actor.get("answered_by"),
                    "service_llm": {
                        "id": trace_actor.get("service_id"),
                        "name": trace_actor.get("service_name"),
                    },
                    "inputs": input_snapshot,
                    "error": str(e),
                    "fallback_output": "I'm having trouble processing that right now. Could you please try again?",
                    "caller": caller,
                    "trace_actor": trace_actor,
                    "trace_context": trace_meta,
                }
            )
            try:
                from services.flow_logger import log_llm_call
                log_llm_call(
                    actor=str(trace_actor.get("answered_by") or trace_actor.get("actor") or caller.get("function") or "llm"),
                    messages=messages,
                    response="",
                    model=request_model,
                    temperature=request_temperature,
                    max_tokens=request_max_tokens,
                    duration_ms=duration_ms,
                    status="error",
                    error=str(e),
                    trace_context=trace_meta,
                )
            except Exception:
                pass
            return "I'm having trouble processing that right now. Could you please try again?"

    async def chat_with_json(
        self,
        messages: list[dict],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        trace_context: Optional[dict[str, Any]] = None,
        max_tokens: Optional[int] = None,
    ) -> dict:
        """
        Send chat request expecting JSON response.

        Returns:
            Parsed JSON dict
        """
        started = time.perf_counter()
        request_id = uuid.uuid4().hex
        caller = self._caller_context()
        trace_meta = trace_context if isinstance(trace_context, dict) else {}
        trace_actor = self._resolve_trace_actor(caller, trace_meta)
        request_messages = self._ensure_json_keyword_for_json_mode(messages)
        input_snapshot = self._extract_message_inputs(request_messages)
        request_model = model or self.model
        request_temperature = temperature if temperature is not None else self.temperature
        request_max_tokens = max_tokens or self.max_tokens
        user_query = self._extract_user_query(request_messages)
        response: Any = None
        content = "{}"
        try:
            response = await self.client.chat.completions.create(
                model=request_model,
                messages=request_messages,
                temperature=request_temperature,
                max_tokens=request_max_tokens,
                response_format={"type": "json_object"},
            )
            content = response.choices[0].message.content or "{}"
            parsed = json.loads(content)
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            usage = self._response_usage(response)
            self._log_llm_simple_call(
                call_id=request_id,
                operation="chat_with_json",
                model=request_model,
                duration_ms=duration_ms,
                status="success",
                trace_context=trace_meta,
                caller=caller,
                usage=usage,
                input_preview={"messages": request_messages, "user_query": user_query},
                output_preview=parsed,
            )
            self._log_llm_trace(
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "request_id": request_id,
                    "method": "chat_with_json",
                    "status": "success",
                    "duration_ms": duration_ms,
                    "model": request_model,
                    "temperature": request_temperature,
                    "max_tokens": self.max_tokens,
                    "user_query": user_query,
                    "answered_by": trace_actor.get("answered_by"),
                    "service_llm": {
                        "id": trace_actor.get("service_id"),
                        "name": trace_actor.get("service_name"),
                    },
                    "inputs": input_snapshot,
                    "output": parsed,
                    "raw_output": content,
                    "usage": usage,
                    "caller": caller,
                    "trace_actor": trace_actor,
                    "trace_context": trace_meta,
                }
            )
            return parsed
        except json.JSONDecodeError as e:
            try:
                print(f"JSON Parse Error: {e}")
            except OSError:
                pass
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            self._log_llm_simple_call(
                call_id=request_id,
                operation="chat_with_json",
                model=request_model,
                duration_ms=duration_ms,
                status="json_parse_error",
                trace_context=trace_meta,
                caller=caller,
                usage=self._response_usage(response) if response is not None else {},
                input_preview={"messages": request_messages, "user_query": user_query},
                output_preview=content,
                error=str(e),
            )
            self._log_llm_trace(
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "request_id": request_id,
                    "method": "chat_with_json",
                    "status": "json_parse_error",
                    "duration_ms": duration_ms,
                    "model": request_model,
                    "temperature": request_temperature,
                    "max_tokens": self.max_tokens,
                    "user_query": user_query,
                    "answered_by": trace_actor.get("answered_by"),
                    "service_llm": {
                        "id": trace_actor.get("service_id"),
                        "name": trace_actor.get("service_name"),
                    },
                    "inputs": input_snapshot,
                    "error": str(e),
                    "fallback_output": {"intent": "unclear", "confidence": 0.3, "entities": {}},
                    "caller": caller,
                    "trace_actor": trace_actor,
                    "trace_context": trace_meta,
                }
            )
            return {"intent": "unclear", "confidence": 0.3, "entities": {}}
        except Exception as e:
            try:
                print(f"LLM Error: {e}")
            except OSError:
                pass
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            self._log_llm_simple_call(
                call_id=request_id,
                operation="chat_with_json",
                model=request_model,
                duration_ms=duration_ms,
                status="error",
                trace_context=trace_meta,
                caller=caller,
                usage=self._response_usage(response) if response is not None else {},
                input_preview={"messages": request_messages, "user_query": user_query},
                output_preview=content,
                error=str(e),
            )
            self._log_llm_trace(
                {
                    "timestamp": datetime.now(UTC).isoformat(),
                    "request_id": request_id,
                    "method": "chat_with_json",
                    "status": "error",
                    "duration_ms": duration_ms,
                    "model": request_model,
                    "temperature": request_temperature,
                    "max_tokens": self.max_tokens,
                    "user_query": user_query,
                    "answered_by": trace_actor.get("answered_by"),
                    "service_llm": {
                        "id": trace_actor.get("service_id"),
                        "name": trace_actor.get("service_name"),
                    },
                    "inputs": input_snapshot,
                    "error": str(e),
                    "fallback_output": {"intent": "unclear", "confidence": 0.3, "entities": {}},
                    "caller": caller,
                    "trace_actor": trace_actor,
                    "trace_context": trace_meta,
                }
            )
            return {"intent": "unclear", "confidence": 0.3, "entities": {}}

    async def raw_chat_completion(
        self,
        *,
        trace_context: Optional[dict[str, Any]] = None,
        purpose: str = "",
        **request_kwargs: Any,
    ) -> Any:
        """
        Proxy chat.completions.create with simple call logging.
        Raises original exceptions so caller behavior remains unchanged.
        """
        started = time.perf_counter()
        request_id = uuid.uuid4().hex
        caller = self._caller_context()
        trace_meta = trace_context if isinstance(trace_context, dict) else {}
        request_model = str(request_kwargs.get("model") or self.model).strip() or self.model

        try:
            response = await self.client.chat.completions.create(**request_kwargs)
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            usage = self._response_usage(response)
            output_preview = ""
            try:
                output_preview = str(response.choices[0].message.content or "").strip()
            except Exception:
                output_preview = ""
            self._log_llm_simple_call(
                call_id=request_id,
                operation="chat_completion",
                model=request_model,
                duration_ms=duration_ms,
                status="success",
                trace_context=trace_meta,
                caller=caller,
                usage=usage,
                purpose=purpose,
                input_preview={
                    "messages": request_kwargs.get("messages"),
                    "request_kwargs": {
                        key: value
                        for key, value in request_kwargs.items()
                        if key != "messages"
                    },
                },
                output_preview=output_preview,
            )
            return response
        except Exception as exc:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            self._log_llm_simple_call(
                call_id=request_id,
                operation="chat_completion",
                model=request_model,
                duration_ms=duration_ms,
                status="error",
                trace_context=trace_meta,
                caller=caller,
                usage={},
                purpose=purpose,
                input_preview={
                    "messages": request_kwargs.get("messages"),
                    "request_kwargs": {
                        key: value
                        for key, value in request_kwargs.items()
                        if key != "messages"
                    },
                },
                output_preview="",
                error=str(exc),
            )
            raise

    async def raw_embeddings_create(
        self,
        *,
        trace_context: Optional[dict[str, Any]] = None,
        purpose: str = "",
        **request_kwargs: Any,
    ) -> Any:
        """
        Proxy embeddings.create with simple call logging.
        Raises original exceptions so caller behavior remains unchanged.
        """
        started = time.perf_counter()
        request_id = uuid.uuid4().hex
        caller = self._caller_context()
        trace_meta = trace_context if isinstance(trace_context, dict) else {}
        request_model = str(
            request_kwargs.get("model") or getattr(settings, "openai_embedding_model", "")
        ).strip() or "text-embedding-3-small"

        try:
            response = await self.client.embeddings.create(**request_kwargs)
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            usage = self._response_usage(response)
            output_preview = f"embedding_count={len(getattr(response, 'data', []) or [])}"
            self._log_llm_simple_call(
                call_id=request_id,
                operation="embedding",
                model=request_model,
                duration_ms=duration_ms,
                status="success",
                trace_context=trace_meta,
                caller=caller,
                usage=usage,
                purpose=purpose,
                input_preview={
                    "input": request_kwargs.get("input"),
                    "request_kwargs": {
                        key: value
                        for key, value in request_kwargs.items()
                        if key != "input"
                    },
                },
                output_preview=output_preview,
            )
            return response
        except Exception as exc:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            self._log_llm_simple_call(
                call_id=request_id,
                operation="embedding",
                model=request_model,
                duration_ms=duration_ms,
                status="error",
                trace_context=trace_meta,
                caller=caller,
                usage={},
                purpose=purpose,
                input_preview={
                    "input": request_kwargs.get("input"),
                    "request_kwargs": {
                        key: value
                        for key, value in request_kwargs.items()
                        if key != "input"
                    },
                },
                output_preview="",
                error=str(exc),
            )
            raise

    async def classify_intent(
        self,
        user_message: str,
        conversation_history: list[dict],
        context: dict,
    ) -> dict:
        """
        Classify user intent with confidence score.

        Returns:
            {
                "intent": str,
                "confidence": float,
                "entities": dict,
                "reasoning": str
            }
        """
        prompt_config = config_service.get_prompts()
        nlu_policy = config_service.get_nlu_policy()
        classifier_prompt = str(prompt_config.get("classifier_prompt", "")).strip()
        nlu_dos = self._list_or_fallback(nlu_policy.get("dos", []), "Classify based on the closest supported workflow.")
        nlu_donts = self._list_or_fallback(nlu_policy.get("donts", []), "Do not fabricate unsupported workflows.")
        intent_catalog = context.get("intent_catalog") or context.get("capabilities", {}).get("intents", [])
        service_catalog = context.get("service_catalog") or context.get("capabilities", {}).get("service_catalog", [])
        faq_bank = context.get("faq_bank") or context.get("capabilities", {}).get("faq_bank", [])
        tools = context.get("tools") or context.get("capabilities", {}).get("tools", [])
        memory_summary = str(context.get("conversation_summary", "")).strip()
        memory_facts = context.get("memory_facts", {})
        if not isinstance(memory_facts, dict):
            memory_facts = {}
        memory_recent_changes = context.get("memory_recent_changes", [])
        if not isinstance(memory_recent_changes, list):
            memory_recent_changes = []
        context_pack = context.get("context_pack", {})
        if not isinstance(context_pack, dict):
            context_pack = {}
        selected_phase_id = str(
            context.get("selected_phase_id")
            or context.get("phase_id")
            or ""
        ).strip().lower().replace(" ", "_")
        selected_phase_name = str(
            context.get("selected_phase_name")
            or context.get("phase_name")
            or ""
        ).strip()
        if not selected_phase_name and selected_phase_id:
            selected_phase_name = selected_phase_id.replace("_", " ").title()

        intent_lines = []
        for intent_cfg in intent_catalog:
            if not isinstance(intent_cfg, dict):
                continue
            intent_id = str(intent_cfg.get("id", "")).strip()
            if not intent_id:
                continue
            label = str(intent_cfg.get("label") or intent_id).strip()
            enabled = bool(intent_cfg.get("enabled", True))
            maps_to = str(intent_cfg.get("maps_to") or "").strip()
            mapping_text = f", maps_to={maps_to}" if maps_to else ""
            intent_lines.append(f"- {intent_id} ({label}) enabled={enabled}{mapping_text}")
        intent_catalog_str = "\n".join(intent_lines) if intent_lines else "- No custom intent catalog configured."

        service_lines = []
        for service in service_catalog:
            if not isinstance(service, dict):
                continue
            service_id = str(service.get("id", "")).strip()
            name = str(service.get("name", "")).strip()
            if not service_id and not name:
                continue
            service_type = str(service.get("type", "service")).strip()
            description = str(service.get("description") or service.get("cuisine") or "").strip()
            active = bool(service.get("is_active", True))
            status = "active" if active else "inactive"
            label = f"{name} [{service_id}]" if name and service_id else (name or service_id)
            detail = f": {description}" if description else ""
            service_lines.append(f"- {label} ({service_type}, {status}){detail}")
        service_catalog_str = "\n".join(service_lines) if service_lines else "- No service catalog configured."

        faq_lines = []
        for faq in faq_bank:
            if not isinstance(faq, dict):
                continue
            faq_id = str(faq.get("id") or "").strip()
            question = str(faq.get("question") or "").strip()
            if not faq_id and not question:
                continue
            status = "enabled" if faq.get("enabled", True) else "disabled"
            detail = f"{question}" if question else faq_id
            faq_lines.append(f"- {detail} [{faq_id or 'no_id'}] ({status})")
        faq_bank_str = "\n".join(faq_lines) if faq_lines else "- No FAQ bank entries configured."

        tool_lines = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            tool_id = str(tool.get("id") or "").strip()
            name = str(tool.get("name") or "").strip()
            if not tool_id and not name:
                continue
            status = "enabled" if tool.get("enabled", True) else "disabled"
            tool_type = str(tool.get("type") or "workflow").strip()
            label = f"{name} [{tool_id}]" if name and tool_id else (name or tool_id)
            tool_lines.append(f"- {label} ({tool_type}, {status})")
        tools_str = "\n".join(tool_lines) if tool_lines else "- No tools configured."

        # Build history string (full available history by default).
        history_str = self._build_history_string(conversation_history)

        from services.prompt_registry_service import (
            PromptMissingError,
            prompt_registry,
        )
        try:
            formatted_prompt = await prompt_registry.get(
                "chat.classify_intent",
                {
                    "hotel_name": context.get("hotel_name", context.get("hotel_code", "Hotel")),
                    "business_type": context.get("business_type", "generic"),
                    "guest_name": context.get("guest_name", "Guest"),
                    "state": context.get("state", "idle"),
                    "pending_action": context.get("pending_action", "None"),
                    "selected_phase_name": selected_phase_name or "Unknown",
                    "selected_phase_id": selected_phase_id or "unknown",
                    "enabled_intents": ", ".join(context.get("enabled_intents", [])) or "not provided",
                    "conversation_summary": memory_summary or "No long-term summary yet.",
                    "memory_facts": json.dumps(memory_facts, ensure_ascii=False),
                    "memory_recent_changes": json.dumps(memory_recent_changes[-5:], ensure_ascii=False),
                    "context_pack": json.dumps(context_pack, ensure_ascii=False),
                    "intent_catalog": intent_catalog_str,
                    "service_catalog": service_catalog_str,
                    "faq_bank": faq_bank_str,
                    "tools": tools_str,
                    "classifier_prompt": classifier_prompt or "None configured.",
                    "nlu_dos": nlu_dos,
                    "nlu_donts": nlu_donts,
                    "history": history_str,
                },
            )
        except PromptMissingError:
            logging.getLogger(__name__).exception("chat_classify_intent_prompt_missing")
            return {"intent": "unclear", "confidence": 0.3, "entities": {}}

        messages = [
            {"role": "system", "content": formatted_prompt},
            {"role": "user", "content": user_message},
        ]

        return await self.chat_with_json(messages, temperature=0.1)

    async def generate_response(
        self,
        user_message: str,
        intent: str,
        entities: dict,
        conversation_history: list[dict],
        context: dict,
    ) -> str:
        """
        Generate contextual response based on intent.

        Returns:
            Response text for the user
        """
        # Extract capabilities from context
        capabilities = context.get("capabilities", {})
        capability_flags = capabilities.get("services", {})
        capability_configs = capabilities.get("capabilities", {})
        service_catalog = capabilities.get("service_catalog", [])
        faq_bank = capabilities.get("faq_bank", [])
        tools = capabilities.get("tools", [])
        business_type = context.get("business_type", "generic")

        # Build dynamic capability list
        caps_list = []
        for capability_id, capability_data in capability_configs.items():
            if not isinstance(capability_data, dict):
                continue
            if not capability_data.get("enabled", False):
                continue
            label = str(capability_id).replace("_", " ").title()
            description = str(capability_data.get("description") or "").strip()
            hours = str(capability_data.get("hours") or "").strip()
            extras = [part for part in (description, f"hours: {hours}" if hours else "") if part]
            suffix = f" ({'; '.join(extras)})" if extras else ""
            caps_list.append(f"- {label}: Available{suffix}")

        if not caps_list:
            for capability_id, enabled in capability_flags.items():
                if capability_id.endswith("_hours") or not enabled:
                    continue
                label = str(capability_id).replace("_", " ").title()
                hours = str(capability_flags.get(f"{capability_id}_hours") or "").strip()
                suffix = f" (hours: {hours})" if hours else ""
                caps_list.append(f"- {label}: Available{suffix}")

        caps_str = "\n".join(caps_list) if caps_list else "No enabled capabilities configured."

        # Build dynamic service catalog list
        service_lines = []
        for service in service_catalog:
            if not isinstance(service, dict):
                continue
            service_name = str(service.get("name") or "").strip()
            service_id = str(service.get("id") or "").strip()
            if not service_name and not service_id:
                continue
            service_type = str(service.get("type") or "service").strip()
            description = str(service.get("description") or service.get("cuisine") or "").strip()
            is_active = bool(service.get("is_active", True))
            status = "active" if is_active else "inactive"

            hours_value = service.get("hours")
            if isinstance(hours_value, dict):
                open_time = str(hours_value.get("open") or "").strip()
                close_time = str(hours_value.get("close") or "").strip()
                hours_text = f"{open_time}-{close_time}" if (open_time or close_time) else "not specified"
            else:
                hours_text = str(hours_value or "not specified").strip()

            delivery_zones = service.get("delivery_zones") or []
            delivery_text = ", ".join(str(zone) for zone in delivery_zones) if delivery_zones else "unspecified"
            heading = f"{service_name} [{service_id}]" if service_name and service_id else (service_name or service_id)
            detail_parts = [f"type={service_type}", f"status={status}", f"hours={hours_text}", f"zones={delivery_text}"]
            if description:
                detail_parts.append(f"description={description}")
            service_lines.append(f"- {heading}: " + ", ".join(detail_parts))

        services_str = "\n".join(service_lines) if service_lines else "No services configured."

        faq_lines = []
        for faq in faq_bank:
            if not isinstance(faq, dict):
                continue
            if not faq.get("enabled", True):
                continue
            question = str(faq.get("question") or "").strip()
            answer = str(faq.get("answer") or "").strip()
            faq_id = str(faq.get("id") or "").strip()
            if not question or not answer:
                continue
            faq_lines.append(f"- [{faq_id}] Q: {question} | A: {answer}")
        faq_bank_str = "\n".join(faq_lines) if faq_lines else "No enabled FAQ bank entries."

        tool_lines = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            name = str(tool.get("name") or "").strip()
            tool_id = str(tool.get("id") or "").strip()
            if not name and not tool_id:
                continue
            label = f"{name} [{tool_id}]" if name and tool_id else (name or tool_id)
            status = "enabled" if tool.get("enabled", True) else "disabled"
            tool_type = str(tool.get("type") or "workflow").strip()
            description = str(tool.get("description") or "").strip()
            suffix = f": {description}" if description else ""
            tool_lines.append(f"- {label} ({tool_type}, {status}){suffix}")
        tools_str = "\n".join(tool_lines) if tool_lines else "No tools configured."

        prompt_config = config_service.get_prompts()
        nlu_policy = config_service.get_nlu_policy()
        admin_system_prompt = self._render_admin_prompt(
            str(prompt_config.get("system_prompt", "")).strip(),
            context,
        )
        response_style = str(prompt_config.get("response_style", "")).strip()
        nlu_dos = self._list_or_fallback(nlu_policy.get("dos", []), "Stay aligned with configured capabilities.")
        nlu_donts = self._list_or_fallback(nlu_policy.get("donts", []), "Do not promise unavailable actions.")
        memory_summary = str(context.get("conversation_summary", "")).strip()
        memory_facts = context.get("memory_facts", {})
        if not isinstance(memory_facts, dict):
            memory_facts = {}
        memory_recent_changes = context.get("memory_recent_changes", [])
        if not isinstance(memory_recent_changes, list):
            memory_recent_changes = []
        context_pack = context.get("context_pack", {})
        if not isinstance(context_pack, dict):
            context_pack = {}
        selected_phase_id = str(
            context.get("selected_phase_id")
            or context.get("phase_id")
            or ""
        ).strip().lower().replace(" ", "_")
        selected_phase_name = str(
            context.get("selected_phase_name")
            or context.get("phase_name")
            or ""
        ).strip()
        if not selected_phase_name and selected_phase_id:
            selected_phase_name = selected_phase_id.replace("_", " ").title()

        # Build history string (full available history by default).
        history_str = self._build_history_string(conversation_history)

        # Get bot_name from capabilities (synced from admin portal config)
        bot_name = capabilities.get("bot_name", "Assistant")

        from services.prompt_registry_service import (
            PromptMissingError,
            prompt_registry,
        )
        try:
            formatted_prompt = await prompt_registry.get(
                "chat.generate_response",
                {
                    "bot_name": bot_name,
                    "hotel_name": context.get("hotel_name", context.get("hotel_code", "Hotel")).replace("_", " "),
                    "business_type": business_type,
                    "city": context.get("city", ""),
                    "admin_system_prompt": admin_system_prompt or "No custom system prompt configured.",
                    "capabilities_str": caps_str,
                    "services_str": services_str,
                    "faq_bank": faq_bank_str,
                    "tools": tools_str,
                    "nlu_dos": nlu_dos,
                    "nlu_donts": nlu_donts,
                    "guest_name": context.get("guest_name", "Guest"),
                    "room_number": context.get("room_number", "Not specified"),
                    "state": context.get("state", "idle"),
                    "pending_action": context.get("pending_action", "None"),
                    "selected_phase_name": selected_phase_name or "Unknown",
                    "selected_phase_id": selected_phase_id or "unknown",
                    "conversation_summary": memory_summary or "No long-term summary yet.",
                    "memory_facts": json.dumps(memory_facts, ensure_ascii=False),
                    "memory_recent_changes": json.dumps(memory_recent_changes[-5:], ensure_ascii=False),
                    "context_pack": json.dumps(context_pack, ensure_ascii=False),
                    "intent": intent,
                    "entities": json.dumps(entities),
                    "response_style": response_style or "Default",
                    "history": history_str,
                },
            )
        except PromptMissingError:
            logging.getLogger(__name__).exception("chat_generate_response_prompt_missing")
            return ""

        _faq_block_gen = build_faq_block()
        if _faq_block_gen:
            formatted_prompt = _faq_block_gen + "\n\n" + formatted_prompt

        messages = [
            {"role": "system", "content": formatted_prompt},
            {"role": "user", "content": user_message},
        ]

        return await self.chat(messages, temperature=0.7)


# Global instance
llm_client = LLMClient()
