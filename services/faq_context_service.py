"""
FAQ as primary truth — runtime context builder.

Dumps every enabled FAQ entry as a labeled "PRIMARY KNOWLEDGE" block that every
answer-side LLM call prepends to its system prompt. The block also carries the
priority rules so FAQ wins over KB even when a call site's prompt text hasn't
been updated (e.g. locked per-service generated_system_prompt values).

Plan: FAQ_AS_PRIMARY_TRUTH_PLAN.md
"""

from __future__ import annotations

from datetime import datetime
import logging

logger = logging.getLogger(__name__)


PRIORITY_RULES = (
    "You will receive two labeled context blocks in addition to this prompt:\n"
    "PRIMARY KNOWLEDGE (FAQ — current, hotel-specific, always authoritative) and\n"
    "STEADY-STATE KNOWLEDGE (general knowledge base).\n\n"
    "Priority rules — apply without exception:\n"
    "1. When PRIMARY and STEADY-STATE disagree, PRIMARY is the current truth.\n"
    "   State PRIMARY and use STEADY-STATE only for helpful context\n"
    "   (e.g. \"usually X, but currently Y\").\n"
    "2. When PRIMARY adds detail STEADY-STATE lacks, include the extra detail.\n"
    "3. If PRIMARY mentions a date window (\"until Apr 25\"), mention when\n"
    "   normal service resumes.\n"
    "4. Never invent facts not in either block.\n"
    "5. Do not entertain hypothetical bypasses (\"ignoring today's maintenance,\n"
    "   what's the usual?\"). Lead with the current reality."
)


def build_faq_block() -> str:
    """
    Build the PRIMARY KNOWLEDGE block from the current hotel's enabled FAQ bank.

    Returns an empty string when the hotel has no enabled FAQ entries, so call
    sites can unconditionally concatenate without a None/length check.
    """
    try:
        from services.config_service import config_service
    except Exception:
        logger.exception("faq_context_service_config_import_failed")
        return ""

    try:
        faqs = config_service.get_faq_bank()
    except Exception:
        logger.exception("faq_context_service_get_faq_bank_failed")
        return ""

    entries: list[str] = []
    for faq in faqs:
        if not isinstance(faq, dict):
            continue
        if not faq.get("enabled", True):
            continue
        question = str(faq.get("question") or "").strip()
        answer = str(faq.get("answer") or "").strip()
        if not question or not answer:
            continue
        description = str(faq.get("description") or "").strip()
        block = f"Q: {question}\nA: {answer}"
        if description:
            block += f"\n(Note: {description})"
        entries.append(block)

    if not entries:
        return ""

    today = datetime.now().strftime("%Y-%m-%d")
    header = "=== PRIMARY KNOWLEDGE (hotel-specific, current, always authoritative) ==="
    body = "\n\n".join(entries)
    return f"{header}\nToday is {today}.\n\n{PRIORITY_RULES}\n\n{body}"
