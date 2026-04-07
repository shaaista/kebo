"""
Response Beautifier Service

Builds display-only variants of assistant text.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from config.settings import settings
from llm.client import llm_client

class ResponseBeautifierService:
    """Format outbound assistant text for cleaner chat presentation."""

    _COLLECTION_REQUEST_PATTERN = re.compile(
        r"(?P<lemma>(?:could\s+you\s+)?(?:please|kindly)\s+share(?:\s+your)?\s+)"
        r"(?P<fields>[^.?!\n]+)"
        r"(?P<end>[.?!]?)",
        flags=re.IGNORECASE,
    )

    @staticmethod
    def _strip_markdown_headers(text: str) -> str:
        """Convert markdown headers (# / ## / ###) to plain text lines."""
        return re.sub(r"(?m)^#{1,6}\s+", "", str(text or ""))

    @staticmethod
    def _strip_markdown_emphasis(text: str) -> str:
        cleaned = str(text or "")
        cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", cleaned, flags=re.DOTALL)
        cleaned = re.sub(r"__(.*?)__", r"\1", cleaned, flags=re.DOTALL)
        cleaned = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", cleaned)
        cleaned = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"\1", cleaned)
        return cleaned

    @staticmethod
    def _normalize_bullet_markers(text: str) -> str:
        lines: list[str] = []
        for raw_line in str(text or "").split("\n"):
            stripped = raw_line.strip()
            if not stripped:
                lines.append("")
                continue
            if re.match(r"^[-*]\s+", stripped):
                item = re.sub(r"^[-*]\s+", "", stripped)
                lines.append(f"- {item}")
                continue
            if stripped.startswith("• "):
                lines.append(f"- {stripped[2:].strip()}")
                continue
            lines.append(raw_line.rstrip())
        return "\n".join(lines)

    @staticmethod
    def _split_detail_fields(raw_fields: str) -> list[str]:
        fields = str(raw_fields or "").strip()
        if not fields:
            return []

        # Remove trailing purpose clause for cleaner bullets.
        fields = re.split(
            r"\bso\s+(?:that\s+)?(?:i|we)\b|\bto\s+(?:help|proceed|continue|forward)\b",
            fields,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip(" .,:;")
        if not fields:
            return []

        normalized = re.sub(r"\s+\band\b\s+", ", ", fields, flags=re.IGNORECASE)
        parts = [segment.strip(" .,:;") for segment in normalized.split(",")]
        cleaned_parts: list[str] = []
        for part in parts:
            if not part:
                continue
            item = re.sub(r"^(?:the|your)\s+", "", part, flags=re.IGNORECASE).strip(" .,:;")
            if not item:
                continue
            cleaned_parts.append(item)
        return cleaned_parts

    def _format_collection_request(self, text: str) -> str:
        source = str(text or "")
        match = self._COLLECTION_REQUEST_PATTERN.search(source)
        if not match:
            return source

        fields_segment = str(match.group("fields") or "")
        example_note = ""
        example_parts = re.findall(r"\(([^)]*)\)", fields_segment)
        if example_parts:
            for raw_note in example_parts:
                if re.search(r"(?:for\s+example|e\.?g\.?)", raw_note, flags=re.IGNORECASE):
                    example_note = re.sub(
                        r"^(?:for\s+example|e\.?g\.?)[\s:.-]*",
                        "",
                        raw_note.strip(),
                        flags=re.IGNORECASE,
                    ).strip()
                    break
        fields_without_notes = re.sub(r"\([^)]*\)", "", fields_segment).strip()

        parts = self._split_detail_fields(fields_without_notes)
        if len(parts) < 2:
            return source

        prefix = source[: match.start()].rstrip().rstrip(",;:")
        suffix = source[match.end() :].lstrip()

        bullets = "\n".join(f"- {item}" for item in parts)
        replacement = f"Please share the following details:\n{bullets}"
        if example_note:
            replacement = f"{replacement}\nExample: {example_note}"

        rebuilt = replacement
        if prefix:
            rebuilt = f"{prefix}\n{replacement}"
        if suffix:
            rebuilt = f"{rebuilt}\n{suffix}"
        return rebuilt

    @staticmethod
    def _cleanup_spacing(text: str) -> str:
        compact = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
        compact = re.sub(r"\n{3,}", "\n\n", compact)
        compact = re.sub(r"[ \t]+\n", "\n", compact)
        return compact.strip()

    def beautify_response_text(self, text: str) -> str:
        if not str(text or "").strip():
            return ""
        cleaned = self._strip_markdown_headers(text)
        cleaned = self._strip_markdown_emphasis(cleaned)
        cleaned = self._normalize_bullet_markers(cleaned)
        cleaned = self._format_collection_request(cleaned)
        cleaned = self._cleanup_spacing(cleaned)
        return cleaned

    @staticmethod
    def _comparison_text(value: str) -> str:
        text = str(value or "").strip().lower()
        text = re.sub(r"\s+", " ", text)
        return text

    @staticmethod
    def _extract_number_tokens(value: str) -> set[str]:
        return set(re.findall(r"\b\d+\b", str(value or "")))

    @staticmethod
    def _extract_word_tokens(value: str) -> set[str]:
        return {
            token
            for token in re.findall(r"[a-z0-9]+", str(value or "").lower())
            if len(token) >= 2
        }

    @staticmethod
    def _looks_dense_single_block(text: str) -> bool:
        compact = str(text or "").strip()
        if not compact:
            return False
        return len(compact) >= 320 and compact.count("\n") <= 1

    @staticmethod
    def _is_structured_chat_style(text: str) -> bool:
        content = str(text or "").strip()
        if not content:
            return False
        if content.count("\n") < 1:
            return False
        if re.search(r"(?m)^\s*[-*]\s+\S+", content):
            return True
        if re.search(r"(?m)^\s*\d+\.\s+\S+", content):
            return True
        if re.search(r"(?m)^\s*[A-Za-z][^:\n]{2,40}:\s*$", content):
            return True
        if re.search(r"(?m)^\s*\*\*[^*\n]{2,60}\*\*:?$", content):
            return True
        return False

    def _rewrite_guardrails_pass(
        self,
        *,
        base_text: str,
        candidate_text: str,
    ) -> tuple[bool, str]:
        base = self._comparison_text(base_text)
        candidate = self._comparison_text(candidate_text)
        if not candidate:
            return False, "empty_candidate"
        if candidate.startswith("i'm having trouble processing that right now"):
            return False, "llm_fallback_message"
        if "```" in candidate_text:
            return False, "contains_code_fence"

        base_len = len(base)
        candidate_len = len(candidate)
        if base_len > 0:
            if candidate_len > max(1800, int(base_len * 2.5)):
                return False, "candidate_too_long"
            if candidate_len < max(8, int(base_len * 0.25)):
                return False, "candidate_too_short"

        base_tokens = self._extract_word_tokens(base_text)
        overlap_ratio = 0.0
        if base_tokens:
            candidate_tokens = self._extract_word_tokens(candidate_text)
            overlap_ratio = len(base_tokens & candidate_tokens) / max(1, len(base_tokens))
            if overlap_ratio < 0.68:
                return False, "low_token_overlap"

        similarity = SequenceMatcher(a=base, b=candidate).ratio()
        min_similarity = float(getattr(settings, "chat_display_beautifier_min_similarity", 0.45) or 0.45)
        normalized_min_similarity = max(0.08, min(0.9, min_similarity))
        if similarity < normalized_min_similarity and overlap_ratio < 0.68:
            return False, "low_similarity"

        confirmation_phrase = str(getattr(settings, "chat_confirmation_phrase", "yes confirm") or "yes confirm").strip()
        if confirmation_phrase:
            phrase_lower = confirmation_phrase.lower()
            in_base = phrase_lower in base
            in_candidate = phrase_lower in candidate
            if in_base and not in_candidate:
                return False, "confirmation_phrase_lost"
            if not in_base and in_candidate:
                return False, "confirmation_phrase_added"

        base_numbers = self._extract_number_tokens(base_text)
        if base_numbers:
            candidate_numbers = self._extract_number_tokens(candidate_text)
            retained = len(base_numbers & candidate_numbers)
            if retained < len(base_numbers):
                return False, "numeric_details_dropped"

        return True, ""

    async def beautify_display_text(
        self,
        text: str,
        *,
        state: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        base_text = self.beautify_response_text(text)
        meta: dict[str, Any] = {
            "display_beautifier_enabled": bool(getattr(settings, "chat_display_beautifier_enabled", True)),
            "display_beautifier_llm_used": False,
            "display_beautifier_retry_used": False,
            "display_beautifier_applied": False,
            "display_beautifier_fallback_reason": "",
        }

        if not base_text:
            return "", meta
        if not meta["display_beautifier_enabled"]:
            return base_text, meta

        use_llm = bool(getattr(settings, "chat_display_beautifier_use_llm", True))
        if not use_llm:
            meta["display_beautifier_fallback_reason"] = "llm_disabled"
            return base_text, meta
        if not str(getattr(settings, "openai_api_key", "") or "").strip():
            meta["display_beautifier_fallback_reason"] = "missing_openai_api_key"
            return base_text, meta

        response_source = ""
        if isinstance(metadata, dict):
            response_source = str(metadata.get("response_source") or "").strip()
        model = (
            str(getattr(settings, "chat_display_beautifier_model", "") or "").strip()
            or str(getattr(settings, "chat_llm_response_surface_model", "") or "").strip()
            or None
        )
        temperature = float(getattr(settings, "chat_display_beautifier_temperature", 0.2) or 0.2)
        _configured_max = max(80, int(getattr(settings, "chat_display_beautifier_max_tokens", 420) or 420))
        # Scale max_tokens based on input length so long responses (room lists,
        # comparisons) aren't truncated by the beautifier LLM.  Roughly 1 token
        # ≈ 4 chars; we allow ~1.3x headroom for formatting additions.
        _input_estimated_tokens = max(80, len(base_text) // 3)
        max_tokens = max(_configured_max, min(_input_estimated_tokens, 2048))
        confirmation_phrase = str(getattr(settings, "chat_confirmation_phrase", "yes confirm") or "yes confirm").strip()
        confirmation_present = bool(
            confirmation_phrase
            and confirmation_phrase.lower() in self._comparison_text(base_text)
        )

        system_prompt = (
            "You are a display-only formatting editor for concierge replies.\n"
            "Goal: make the response look like clean ChatGPT-style output.\n"
            "Formatting style requirements:\n"
            "- Use short readable blocks.\n"
            "- For dense informational content, use bold labels and bullet points.\n"
            "- Keep wording close to source; this is formatting-first, not rewriting.\n"
            "Hard constraints:\n"
            "- Do NOT use markdown headers (#, ##, ###) — never output lines starting with #.\n"
            "- Do NOT add any new fact or claim.\n"
            "- Do NOT remove any existing fact or claim.\n"
            "- Preserve all numbers, ranges, names, dates, policies, constraints, and IDs.\n"
            "- Keep explicit confirmation phrase unchanged when present.\n"
            "- Return plain text only.\n"
        )
        user_prompt = (
            f"Conversation state: {str(state or '').strip() or 'unknown'}\n"
            f"Response source: {response_source or 'unknown'}\n"
            f"Confirmation phrase in draft: {(confirmation_phrase if confirmation_present else 'NOT_PRESENT')}\n"
            "Do not add the confirmation phrase if it is not present in the draft.\n\n"
            "Please keep the exact content but improve readability and structure.\n\n"
            f"Draft response:\n{base_text}"
        )

        try:
            rewritten = await llm_client.chat(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                trace_context={
                    "actor": "display_beautifier",
                    "service_id": "display_beautifier",
                    "service_name": "Display Beautifier",
                },
            )
        except Exception:
            rewritten = ""

        meta["display_beautifier_llm_used"] = True
        candidate = self._cleanup_spacing(str(rewritten or ""))
        if not candidate:
            meta["display_beautifier_fallback_reason"] = "empty_llm_output"
            return base_text, meta

        passes, reason = self._rewrite_guardrails_pass(
            base_text=base_text,
            candidate_text=candidate,
        )
        if not passes:
            meta["display_beautifier_fallback_reason"] = reason
            return base_text, meta

        if self._looks_dense_single_block(base_text) and not self._is_structured_chat_style(candidate):
            retry_prompt = (
                f"{user_prompt}\n\n"
                "Retry with STRICT formatting:\n"
                "- Start with a short context line.\n"
                "- Then use bullet points for each distinct fact cluster.\n"
                "- Keep all factual content unchanged.\n"
            )
            try:
                retried = await llm_client.chat(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": retry_prompt},
                    ],
                    model=model,
                    temperature=max(0.0, min(temperature, 0.15)),
                    max_tokens=max_tokens,
                    trace_context={
                        "actor": "display_beautifier",
                        "service_id": "display_beautifier",
                        "service_name": "Display Beautifier",
                    },
                )
            except Exception:
                retried = ""
            retry_candidate = self._cleanup_spacing(str(retried or ""))
            if retry_candidate:
                retry_passes, retry_reason = self._rewrite_guardrails_pass(
                    base_text=base_text,
                    candidate_text=retry_candidate,
                )
                if retry_passes and self._is_structured_chat_style(retry_candidate):
                    candidate = retry_candidate
                    meta["display_beautifier_retry_used"] = True
                elif retry_reason:
                    meta["display_beautifier_fallback_reason"] = retry_reason

        meta["display_beautifier_applied"] = (
            self._comparison_text(candidate) != self._comparison_text(base_text)
        )
        return candidate, meta


response_beautifier_service = ResponseBeautifierService()
