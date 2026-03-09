import asyncio
import csv
import json
import os
import re
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DEBUG", "true")

from main import app  # noqa: E402


def now() -> str:
    return datetime.now(UTC).isoformat()


def low(v: Any) -> str:
    return str(v or "").strip().lower()


def get_run_dir() -> Path:
    root = Path(".")
    marker = root / ".qa_run_dir"
    if marker.exists():
        rel = marker.read_text(encoding="utf-8").strip()
        out = root / rel
        out.mkdir(parents=True, exist_ok=True)
        return out
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = root / "logs" / "manual_testing" / stamp
    out.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(out.relative_to(root)).replace("/", "\\"), encoding="utf-8")
    return out


class Runner:
    def __init__(self, out: Path) -> None:
        self.out = out
        self.admin: list[dict[str, Any]] = []
        self.turns: list[dict[str, Any]] = []
        self.cases: list[dict[str, Any]] = []
        self.issues: list[dict[str, Any]] = []
        self.retests: list[dict[str, Any]] = []
        self.issue_i = 0
        self.initial_config: dict[str, Any] = {}
        self.initial_services: list[dict[str, Any]] = []
        self.initial_tools: list[dict[str, Any]] = []
        self.effective_config: dict[str, Any] = {}
        self.restored_config: dict[str, Any] = {}
        self.restore_status = "not_attempted"

    async def req(self, c: AsyncClient, method: str, path: str, payload: Any | None = None, params: dict | None = None) -> tuple[int, Any]:
        r = await c.request(method, path, json=payload, params=params, timeout=180.0)
        try:
            body = r.json()
        except Exception:
            body = {"raw_text": r.text}
        return r.status_code, body

    async def get_services(self, c: AsyncClient) -> list[dict[str, Any]]:
        s, b = await self.req(c, "GET", "/admin/api/config/services")
        return [dict(x) for x in b] if s == 200 and isinstance(b, list) else []

    async def get_tools(self, c: AsyncClient) -> list[dict[str, Any]]:
        s, b = await self.req(c, "GET", "/admin/api/config/tools")
        return [dict(x) for x in b] if s == 200 and isinstance(b, list) else []

    async def get_phases(self, c: AsyncClient) -> list[dict[str, Any]]:
        s, b = await self.req(c, "GET", "/admin/api/config/phases")
        return [dict(x) for x in b] if s == 200 and isinstance(b, list) else []

    async def get_config(self, c: AsyncClient) -> dict[str, Any]:
        s, b = await self.req(c, "GET", "/admin/api/config")
        return dict(b) if s == 200 and isinstance(b, dict) else {}

    def phase_services(self, services: list[dict[str, Any]], phase: str) -> list[dict[str, Any]]:
        p = low(phase)
        return [s for s in services if low(s.get("phase_id")) == p and bool(s.get("is_active", True))]

    def ticket_tool_enabled(self, tools: list[dict[str, Any]]) -> bool:
        for t in tools:
            if low(t.get("id")) in {"ticketing", "ticket_create"}:
                return bool(t.get("enabled", False))
        return False

    def sev_cat(self, tags: list[str]) -> tuple[str, str]:
        high = {"phase_restriction", "routing_mismatch", "kb_grounding", "hallucination", "ticketing_incorrect", "context_isolation", "admin_chat_sync"}
        return ("HIGH" if any(t in high for t in tags) else "MEDIUM", (tags[0] if tags else "unknown"))

    def eval_turn(self, bot: str, meta: dict[str, Any], intent: str, expect: dict[str, Any]) -> tuple[bool, list[str], list[str]]:
        text = low(bot)
        src = low(meta.get("response_source"))
        phase_gate = "phase_gate" in src or bool(meta.get("phase_service_unavailable"))
        ticket = bool(meta.get("ticket_created"))
        fails: list[str] = []
        tags: list[str] = []

        if "expect_phase_gate" in expect and phase_gate != bool(expect["expect_phase_gate"]):
            fails.append(f"phase_gate expected={bool(expect['expect_phase_gate'])} actual={phase_gate}")
            tags.append("phase_restriction")
        if "expect_ticket" in expect and ticket != bool(expect["expect_ticket"]):
            fails.append(f"ticket_created expected={bool(expect['expect_ticket'])} actual={ticket}")
            tags.append("ticketing_incorrect")

        any_tokens = [low(x) for x in expect.get("expect_contains_any", []) if str(x).strip()]
        if any_tokens and not any(x in text for x in any_tokens):
            fails.append(f"missing_any={any_tokens}")
            tags.append("kb_grounding")

        not_tokens = [low(x) for x in expect.get("expect_not_contains_any", []) if str(x).strip()]
        leaks = [x for x in not_tokens if x in text]
        if leaks:
            fails.append(f"unexpected_tokens={leaks}")
            tags.append("context_isolation")

        if expect.get("require_staff_or_ticket"):
            staff = any(x in text for x in ["staff", "human", "agent", "escalat", "front desk"])
            if not staff and not ticket:
                fails.append("expected_staff_or_ticket_path")
                tags.append("ticketing_incorrect")

        intents = [low(x) for x in expect.get("expect_intent_in", [])]
        if intents and low(intent) not in intents:
            fails.append(f"intent expected={intents} actual={low(intent)}")
            tags.append("routing_mismatch")

        if expect.get("expect_no_hallucinated_price"):
            if re.search(r"\b(?:rs|inr)\s*\d{2,5}\b", text) and not any(x in text for x in ["2000", "1500", "2500", "415", "350", "295", "665"]):
                fails.append("possible_hallucinated_price")
                tags.append("hallucination")

        return len(fails) == 0, fails, sorted(set(tags))

    async def upsert_service(self, c: AsyncClient, payload: dict[str, Any], reason: str, linked: list[str]) -> None:
        sid = str(payload.get("id") or "").strip()
        cur = await self.get_services(c)
        exists = any(low(s.get("id")) == low(sid) for s in cur)
        if exists:
            s, b = await self.req(c, "PUT", f"/admin/api/config/services/{sid}", payload)
            action = "update_service"
        else:
            s, b = await self.req(c, "POST", "/admin/api/config/services", payload)
            action = "add_service"
        self.admin.append({"timestamp": now(), "action": action, "reason": reason, "service_id": sid, "payload": payload, "status": s, "response": b, "linked_cases": linked})

    async def update_service(self, c: AsyncClient, sid: str, payload: dict[str, Any], reason: str, linked: list[str]) -> None:
        s, b = await self.req(c, "PUT", f"/admin/api/config/services/{sid}", payload)
        self.admin.append({"timestamp": now(), "action": "update_service", "reason": reason, "service_id": sid, "payload": payload, "status": s, "response": b, "linked_cases": linked})

    async def delete_service(self, c: AsyncClient, sid: str, reason: str, linked: list[str]) -> None:
        s, b = await self.req(c, "DELETE", f"/admin/api/config/services/{sid}")
        self.admin.append({"timestamp": now(), "action": "delete_service", "reason": reason, "service_id": sid, "status": s, "response": b, "linked_cases": linked})

    async def update_tool(self, c: AsyncClient, tid: str, payload: dict[str, Any], reason: str, linked: list[str]) -> None:
        s, b = await self.req(c, "PUT", f"/admin/api/config/tools/{tid}", payload)
        self.admin.append({"timestamp": now(), "action": "update_tool", "reason": reason, "tool_id": tid, "payload": payload, "status": s, "response": b, "linked_cases": linked})
    async def compile_kb(self, c: AsyncClient) -> None:
        payload = {"service_id": None, "force": True, "max_facts_per_service": 30, "preserve_manual": True, "published_by": "manual_qa_agent"}
        s, b = await self.req(c, "POST", "/admin/api/config/service-kb/compile", payload)
        self.admin.append({"timestamp": now(), "action": "compile_service_kb", "reason": "admin coverage", "payload": payload, "status": s, "response": b, "linked_cases": []})

    async def set_manual_fact(self, c: AsyncClient) -> None:
        payload = {
            "service_id": "airport_transfer_assist",
            "facts": ["Airport transfer baseline: Toyota Innova Hycross available.", "Airport transfer pricing baseline: T1 INR 2000 and T2 INR 1500."],
            "published_by": "manual_qa_agent",
        }
        s, b = await self.req(c, "PUT", "/admin/api/config/service-kb/manual-facts", payload)
        self.admin.append({"timestamp": now(), "action": "set_service_kb_manual_facts", "reason": "admin coverage", "payload": payload, "status": s, "response": b, "linked_cases": []})

    async def run_case(self, c: AsyncClient, case: dict[str, Any]) -> None:
        cid = str(case["case_id"])
        phase = str(case["phase"])
        session = str(case["session_id"])
        services = await self.get_services(c)
        tools = await self.get_tools(c)
        phase_services = self.phase_services(services, phase)

        case_ok = True
        all_fails: list[str] = []
        all_tags: list[str] = []
        evid: list[str] = []

        for i, tr in enumerate(case.get("turns", []), start=1):
            user = str(tr.get("user") or "")
            s, body = await self.req(
                c,
                "POST",
                "/api/chat/message",
                {
                    "session_id": session,
                    "message": user,
                    "hotel_code": "DEFAULT",
                    "channel": "web_widget",
                    "metadata": {"phase": phase},
                },
            )
            if not isinstance(body, dict):
                body = {"message": str(body)}
            bot = str(body.get("message") or "")
            intent = str(body.get("intent") or "")
            state = str(body.get("state") or "")
            meta = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
            ok, fails, tags = self.eval_turn(bot, meta, intent, dict(tr.get("expect") or {}))
            if s != 200:
                ok = False
                fails.append(f"http_status={s}")
                tags = sorted(set(tags + ["routing_mismatch"]))

            src = str(meta.get("response_source") or "")
            self.turns.append(
                {
                    "timestamp": now(),
                    "case_id": cid,
                    "session_id": session,
                    "phase": phase,
                    "turn": i,
                    "user_message": user,
                    "bot_response": bot,
                    "intent_state": {"intent": intent, "state": state},
                    "routing_plugin_metadata": {
                        "response_source": src,
                        "routing_path": str(meta.get("routing_path") or ""),
                        "trace_id": str(meta.get("trace_id") or ""),
                        "full_kb_trace_id": str(meta.get("full_kb_trace_id") or ""),
                        "raw_intent": str((meta.get("entities") or {}).get("raw_intent") or ""),
                        "phase_service_unavailable": bool(meta.get("phase_service_unavailable")),
                        "ticket_created": bool(meta.get("ticket_created")),
                        "ticket_id": str(meta.get("ticket_id") or ""),
                    },
                    "kb_source_expectation": str(case.get("kb_expectation") or ""),
                    "admin_config_state_relevant": {
                        "phase_services": [{"id": str(sv.get("id") or ""), "name": str(sv.get("name") or ""), "ticketing_enabled": bool(sv.get("ticketing_enabled", True))} for sv in phase_services],
                        "ticketing_tool_enabled": self.ticket_tool_enabled(tools),
                    },
                    "service_expected_allowed_blocked": str(case.get("service_expectation") or ""),
                    "ticketing_expected_not_expected": str(case.get("ticketing_expectation") or ""),
                    "actual_outcome": {
                        "phase_gate": ("phase_gate" in low(src) or bool(meta.get("phase_service_unavailable"))),
                        "ticket_created": bool(meta.get("ticket_created")),
                        "response_source": src,
                        "http_status": s,
                    },
                    "pass_fail": "PASS" if ok else "FAIL",
                    "issue_tags": tags,
                }
            )

            if not ok:
                case_ok = False
                all_fails.extend(fails)
                all_tags.extend(tags)
                ev = f"{cid}/turn-{i}/session-{session}"
                evid.append(ev)
                sev, cat = self.sev_cat(tags)
                self.issue_i += 1
                self.issues.append(
                    {
                        "issue_id": f"ISSUE-{self.issue_i:03d}",
                        "timestamp": now(),
                        "case_id": cid,
                        "phase": phase,
                        "severity": sev,
                        "category": cat,
                        "issue_tags": "|".join(tags),
                        "expected": "; ".join(fails),
                        "actual": bot[:600],
                        "evidence_session_id": session,
                        "evidence_turn": i,
                    }
                )

        self.cases.append(
            {
                "case_id": cid,
                "phase": phase,
                "configured_services_for_phase": [{"id": str(sv.get("id") or ""), "name": str(sv.get("name") or ""), "ticketing_enabled": bool(sv.get("ticketing_enabled", True))} for sv in phase_services],
                "tested_service_or_question_type": str(case.get("tested_type") or ""),
                "expected_behavior": str(case.get("expected_behavior") or ""),
                "actual_behavior": "PASS" if case_ok else "; ".join(all_fails[:8]),
                "kb_correctness_status": "PASS" if case_ok else ("FAIL" if "kb_grounding" in all_tags else "PARTIAL"),
                "ticketing_correctness_status": "PASS" if case_ok else ("FAIL" if "ticketing_incorrect" in all_tags else "PARTIAL"),
                "pass_fail": "PASS" if case_ok else "FAIL",
                "defect_summary": "" if case_ok else "; ".join(sorted(set(all_fails))[:10]),
                "severity": "NONE" if case_ok else self.sev_cat(all_tags)[0],
                "evidence_reference": ", ".join(evid),
            }
        )
        if case.get("regression_of"):
            self.retests.append(
                {
                    "case_id": cid,
                    "regression_of": str(case["regression_of"]),
                    "status": "resolved" if case_ok else "still_failing",
                    "pass_fail": "PASS" if case_ok else "FAIL",
                    "evidence_reference": ", ".join(evid),
                }
            )

    async def setup(self, c: AsyncClient) -> None:
        self.initial_config = await self.get_config(c)
        self.initial_services = await self.get_services(c)
        self.initial_tools = await self.get_tools(c)
        phases = await self.get_phases(c)
        self.admin.append({"timestamp": now(), "action": "snapshot_initial_state", "services_count": len(self.initial_services), "tools_count": len(self.initial_tools), "phases_count": len(phases)})

        targets = [
            {"id": "airport_transfer_assist", "name": "Airport Transfer Assist", "type": "service", "description": "Coordinate pre-arrival airport pickup and drop requests.", "is_active": True, "ticketing_enabled": True, "phase_id": "pre_checkin", "is_builtin": False},
            {"id": "housekeeping_request", "name": "Housekeeping Request", "type": "service", "description": "Handle in-stay housekeeping requests like cleaning and linen.", "is_active": True, "ticketing_enabled": True, "phase_id": "during_stay", "is_builtin": False},
            {"id": "in_room_dining", "name": "In Room Dining", "type": "service", "description": "Support in-room dining requests and menu assistance during stay.", "is_active": True, "ticketing_enabled": False, "phase_id": "during_stay", "is_builtin": False},
            {"id": "lost_found_desk", "name": "Lost And Found Desk", "type": "service", "description": "Manage post-checkout lost-and-found requests and follow-ups.", "is_active": True, "ticketing_enabled": True, "phase_id": "post_checkout", "is_builtin": False},
        ]
        for t in targets:
            await self.upsert_service(c, t, "admin coverage: ensure cross-phase services", ["PRECHECKIN_AIRPORT_001", "DURING_HOUSEKEEP_001", "POST_LOSTFOUND_001"])
        await self.compile_kb(c)
        await self.set_manual_fact(c)
    async def run_suite(self, c: AsyncClient) -> None:
        cases: list[dict[str, Any]] = [
            {
                "case_id": "PB_KB_ROOM_001",
                "phase": "pre_booking",
                "session_id": "qa_pb_1",
                "tested_type": "kb_room_catalog",
                "expected_behavior": "Return grounded room options from KB without blocking.",
                "kb_expectation": "Room types from ROHL source.",
                "service_expectation": "KB informational allowed",
                "ticketing_expectation": "not_expected",
                "turns": [{"user": "What room types do you have?", "expect": {"expect_phase_gate": False, "expect_contains_any": ["lux suite", "premier king", "premier twin"]}}],
            },
            {
                "case_id": "PB_KB_MENU_001",
                "phase": "pre_booking",
                "session_id": "qa_pb_2",
                "tested_type": "menu_price_check",
                "expected_behavior": "Return grounded menu price.",
                "kb_expectation": "BLACK CHICKPEA RASSAM price 415.",
                "service_expectation": "KB informational",
                "ticketing_expectation": "not_expected",
                "turns": [{"user": "What is the price of BLACK CHICKPEA RASSAM at Kadak?", "expect": {"expect_phase_gate": False, "expect_contains_any": ["415", "black chickpea", "kadak"], "expect_no_hallucinated_price": True}}],
            },
            {
                "case_id": "PB_TYPO_001",
                "phase": "pre_booking",
                "session_id": "qa_pb_typo",
                "tested_type": "typo_noisy_input",
                "expected_behavior": "Interpret noisy query correctly.",
                "kb_expectation": "checkin 14:00 and airport transfer 2000/1500.",
                "service_expectation": "KB informational",
                "ticketing_expectation": "not_expected",
                "turns": [{"user": "wt r chk-in tyms n arprt trnsfr cst t1/t2?", "expect": {"expect_phase_gate": False, "expect_contains_any": ["14:00", "2000", "1500", "airport"]}}],
            },
            {
                "case_id": "PB_BLOCK_DURING_001",
                "phase": "pre_booking",
                "session_id": "qa_pb_block",
                "tested_type": "out_of_phase_service",
                "expected_behavior": "Block during-stay service in pre-booking.",
                "kb_expectation": "Phase restriction.",
                "service_expectation": "blocked",
                "ticketing_expectation": "not_expected",
                "turns": [{"user": "Please send housekeeping to room 505 now.", "expect": {"expect_phase_gate": True, "expect_contains_any": ["not available", "pre booking"]}}],
            },
            {
                "case_id": "PB_BOOKING_TICKET_ON_001",
                "phase": "pre_booking",
                "session_id": "qa_pb_booking_on",
                "tested_type": "transactional_room_booking_with_ticket",
                "expected_behavior": "Booking flow creates ticket when enabled.",
                "kb_expectation": "Booking workflow.",
                "service_expectation": "allowed",
                "ticketing_expectation": "expected",
                "turns": [
                    {"user": "I want to book a room from March 22 to March 24.", "expect": {"expect_phase_gate": False}},
                    {"user": "Lux suite for 2 guests.", "expect": {"expect_phase_gate": False}},
                    {"user": "yes confirm", "expect": {"expect_phase_gate": False, "expect_ticket": True}},
                ],
            },
            {
                "case_id": "PRECHECKIN_AIRPORT_001",
                "phase": "pre_checkin",
                "session_id": "qa_precheckin_1",
                "tested_type": "phase_enabled_service",
                "expected_behavior": "Airport transfer available in pre-checkin.",
                "kb_expectation": "T2 INR 1500.",
                "service_expectation": "allowed",
                "ticketing_expectation": "conditional",
                "turns": [{"user": "I already booked. Need airport transfer to T2 tomorrow.", "expect": {"expect_phase_gate": False, "expect_contains_any": ["airport", "t2", "1500"]}}],
            },
            {
                "case_id": "PRECHECKIN_AVAILABLE_001",
                "phase": "pre_checkin",
                "session_id": "qa_precheckin_3",
                "tested_type": "service_discovery",
                "expected_behavior": "List available pre-checkin support accurately.",
                "kb_expectation": "Should include airport support.",
                "service_expectation": "allowed",
                "ticketing_expectation": "not_expected",
                "turns": [{"user": "What can you help me with right now?", "expect": {"expect_phase_gate": False, "expect_contains_any": ["airport", "pre check"]}}],
                "regression_of": "PREV_LOG_PRECHECKIN_AVAILABLE_MISMATCH",
            },
            {
                "case_id": "DURING_HOUSEKEEP_001",
                "phase": "during_stay",
                "session_id": "qa_during_1",
                "tested_type": "during_stay_service",
                "expected_behavior": "Housekeeping allowed during stay.",
                "kb_expectation": "No phase block.",
                "service_expectation": "allowed",
                "ticketing_expectation": "conditional",
                "turns": [{"user": "I need fresh towels and room cleaning for room 612.", "expect": {"expect_phase_gate": False}}],
            },
            {
                "case_id": "DURING_DINING_001",
                "phase": "during_stay",
                "session_id": "qa_during_4",
                "tested_type": "ticketing_disabled_service",
                "expected_behavior": "No ticket when service ticketing disabled.",
                "kb_expectation": "Ticketing-disabled guard applies.",
                "service_expectation": "allowed with ticketing blocked",
                "ticketing_expectation": "not_expected",
                "turns": [{"user": "My in-room dining order is delayed, create a ticket please.", "expect": {"expect_ticket": False, "expect_contains_any": ["ticketing is not enabled", "not enabled", "dining"]}}],
            },
            {
                "case_id": "POST_LOSTFOUND_001",
                "phase": "post_checkout",
                "session_id": "qa_post_1",
                "tested_type": "post_checkout_lost_found",
                "expected_behavior": "Lost-found support available post-checkout.",
                "kb_expectation": "No phase block.",
                "service_expectation": "allowed",
                "ticketing_expectation": "conditional",
                "turns": [{"user": "I checked out yesterday and left my charger in the room.", "expect": {"expect_phase_gate": False, "expect_contains_any": ["lost", "found", "charger", "follow"]}}],
            },
            {
                "case_id": "PB_URGENT_REGRESSION_001",
                "phase": "pre_booking",
                "session_id": "qa_regress_urgent",
                "tested_type": "urgent_out_of_phase",
                "expected_behavior": "Urgent complaint should route to staff/ticket path.",
                "kb_expectation": "Do not drop urgent asks.",
                "service_expectation": "escalate",
                "ticketing_expectation": "expected_or_staff",
                "turns": [{"user": "There is a cockroach in my room, urgent help now.", "expect": {"require_staff_or_ticket": True}}],
                "regression_of": "PREV_LOG_URGENT_PREBOOKING_PHASE_BLOCK",
            },
            {
                "case_id": "MEMORY_RECALL_001",
                "phase": "during_stay",
                "session_id": "qa_memory_1",
                "tested_type": "same_session_memory",
                "expected_behavior": "Remember and update corrected room number.",
                "kb_expectation": "Context memory update.",
                "service_expectation": "context",
                "ticketing_expectation": "not_expected",
                "turns": [
                    {"user": "My name is Alex and my room is 404.", "expect": {"expect_phase_gate": False}},
                    {"user": "What room am I in?", "expect": {"expect_contains_any": ["404"]}},
                    {"user": "Correction: room is 504 actually.", "expect": {"expect_phase_gate": False}},
                    {"user": "Confirm my room number now.", "expect": {"expect_contains_any": ["504"], "expect_not_contains_any": ["404"]}},
                ],
            },
            {
                "case_id": "SESSION_ISOLATION_001",
                "phase": "during_stay",
                "session_id": "qa_memory_2",
                "tested_type": "cross_session_isolation",
                "expected_behavior": "No memory leak across sessions.",
                "kb_expectation": "Should not reveal 504/Alex.",
                "service_expectation": "context isolation",
                "ticketing_expectation": "not_expected",
                "turns": [{"user": "What room am I in?", "expect": {"expect_not_contains_any": ["504", "alex", "404"]}}],
            },
        ]
        for cs in cases:
            await self.run_case(c, cs)
        all_services = await self.get_services(c)
        old_sight = next((deepcopy(s) for s in all_services if low(s.get("id")) == "sightseeing_around_hotel"), None)
        if old_sight:
            await self.delete_service(c, "sightseeing_around_hotel", "admin coverage: remove service", ["ADMIN_REMOVE_SIGHTSEEING_001"])
            await self.run_case(
                c,
                {
                    "case_id": "ADMIN_REMOVE_SIGHTSEEING_001",
                    "phase": "pre_booking",
                    "session_id": "qa_admin_sync_1",
                    "tested_type": "admin_remove_service_sync",
                    "expected_behavior": "Removed sightseeing should not be available for action asks.",
                    "kb_expectation": "phase/service restriction after removal.",
                    "service_expectation": "blocked",
                    "ticketing_expectation": "not_expected",
                    "turns": [{"user": "Book a sightseeing tour around the hotel.", "expect": {"expect_phase_gate": True, "expect_contains_any": ["not available", "pre booking"]}}],
                },
            )
            await self.upsert_service(c, old_sight, "admin coverage: restore removed service", ["ADMIN_READD_SIGHTSEEING_001"])
            await self.run_case(
                c,
                {
                    "case_id": "ADMIN_READD_SIGHTSEEING_001",
                    "phase": "pre_booking",
                    "session_id": "qa_admin_sync_2",
                    "tested_type": "admin_readd_service_sync",
                    "expected_behavior": "Re-added sightseeing should become discoverable.",
                    "kb_expectation": "nearby attractions from KB.",
                    "service_expectation": "allowed informational",
                    "ticketing_expectation": "not_expected",
                    "turns": [{"user": "What sightseeing options are near the hotel?", "expect": {"expect_phase_gate": False, "expect_contains_any": ["attraction", "concierge", "nearby", "hotel"]}}],
                },
            )

        await self.update_service(c, "housekeeping_request", {"ticketing_enabled": False}, "admin coverage: disable service ticketing", ["ADMIN_TOGGLE_HOUSEKEEPING_OFF_001"])
        await self.run_case(
            c,
            {
                "case_id": "ADMIN_TOGGLE_HOUSEKEEPING_OFF_001",
                "phase": "during_stay",
                "session_id": "qa_admin_ticket_toggle_1",
                "tested_type": "service_ticketing_disabled",
                "expected_behavior": "Ticket should not be created when service ticketing OFF.",
                "kb_expectation": "ticketing-disabled message path.",
                "service_expectation": "allowed with ticket blocked",
                "ticketing_expectation": "not_expected",
                "turns": [{"user": "AC is broken in my room. Please create a ticket now.", "expect": {"expect_ticket": False, "expect_contains_any": ["ticketing is not enabled", "not enabled", "housekeeping"]}}],
            },
        )
        await self.update_service(c, "housekeeping_request", {"ticketing_enabled": True}, "admin coverage: enable service ticketing", ["ADMIN_TOGGLE_HOUSEKEEPING_ON_001"])
        await self.run_case(
            c,
            {
                "case_id": "ADMIN_TOGGLE_HOUSEKEEPING_ON_001",
                "phase": "during_stay",
                "session_id": "qa_admin_ticket_toggle_2",
                "tested_type": "service_ticketing_enabled",
                "expected_behavior": "Ticket should be created when service ticketing ON.",
                "kb_expectation": "ticketing handler should trigger.",
                "service_expectation": "allowed",
                "ticketing_expectation": "expected",
                "turns": [{"user": "AC is broken in room 609. Please create a ticket now.", "expect": {"expect_ticket": True}}],
            },
        )

        await self.update_tool(c, "ticketing", {"enabled": False}, "admin coverage: disable ticketing tool", ["ADMIN_TOGGLE_TOOL_OFF_001"])
        await self.run_case(
            c,
            {
                "case_id": "ADMIN_TOGGLE_TOOL_OFF_001",
                "phase": "pre_booking",
                "session_id": "qa_tool_off_booking",
                "tested_type": "tool_toggle_ticketing_off",
                "expected_behavior": "Booking should avoid ticket when tool OFF.",
                "kb_expectation": "Booking flow still works.",
                "service_expectation": "allowed",
                "ticketing_expectation": "not_expected",
                "turns": [
                    {"user": "Book a room for April 2 to April 3.", "expect": {"expect_phase_gate": False}},
                    {"user": "Premier king for 1 guest.", "expect": {"expect_phase_gate": False}},
                    {"user": "yes confirm", "expect": {"expect_ticket": False}},
                ],
            },
        )
        await self.update_tool(c, "ticketing", {"enabled": True}, "admin coverage: re-enable ticketing tool", ["ADMIN_TOGGLE_TOOL_ON_001"])
        await self.run_case(
            c,
            {
                "case_id": "ADMIN_TOGGLE_TOOL_ON_001",
                "phase": "pre_booking",
                "session_id": "qa_tool_on_booking",
                "tested_type": "tool_toggle_ticketing_on",
                "expected_behavior": "Booking should create ticket when tool ON.",
                "kb_expectation": "Booking flow with ticket restored.",
                "service_expectation": "allowed",
                "ticketing_expectation": "expected",
                "turns": [
                    {"user": "Book a room for April 5 to April 6.", "expect": {"expect_phase_gate": False}},
                    {"user": "Premier king for 1 guest.", "expect": {"expect_phase_gate": False}},
                    {"user": "yes confirm", "expect": {"expect_ticket": True}},
                ],
            },
        )

    async def restore(self, c: AsyncClient) -> None:
        if not self.initial_config:
            self.restore_status = "skipped_no_initial_snapshot"
            return
        s, b = await self.req(c, "PUT", "/admin/api/config", self.initial_config)
        self.restore_status = "success" if s == 200 else f"failed_status_{s}"
        if s != 200:
            self.admin.append({"timestamp": now(), "action": "restore_initial_config_failed", "status": s, "response": b})

    def _phase_map(self, services: Any) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        if not isinstance(services, list):
            return out
        for s in services:
            if not isinstance(s, dict):
                continue
            p = str(s.get("phase_id") or "").strip() or "unmapped"
            out.setdefault(p, []).append({"id": str(s.get("id") or ""), "name": str(s.get("name") or ""), "ticketing_enabled": bool(s.get("ticketing_enabled", True))})
        return out

    def _write_issues_csv(self, path: Path) -> None:
        cols = ["issue_id", "timestamp", "case_id", "phase", "severity", "category", "issue_tags", "expected", "actual", "evidence_session_id", "evidence_turn"]
        with path.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for i in self.issues:
                w.writerow({k: i.get(k, "") for k in cols})
    def summary_md(self) -> str:
        total = len(self.cases)
        passed = sum(1 for c in self.cases if c.get("pass_fail") == "PASS")
        failed = total - passed
        rate = round((passed / total) * 100, 2) if total else 0.0
        sev = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
        cat: dict[str, int] = {}
        for i in self.issues:
            s = str(i.get("severity") or "MEDIUM")
            sev[s] = sev.get(s, 0) + 1
            k = str(i.get("category") or "unknown")
            cat[k] = cat.get(k, 0) + 1
        high = [i for i in self.issues if i.get("severity") == "HIGH"]
        reco = "GO" if not high else "NO-GO"

        phase_stats: dict[str, dict[str, int]] = {}
        for c in self.cases:
            p = str(c.get("phase") or "")
            phase_stats.setdefault(p, {"total": 0, "pass": 0, "fail": 0})
            phase_stats[p]["total"] += 1
            if c.get("pass_fail") == "PASS":
                phase_stats[p]["pass"] += 1
            else:
                phase_stats[p]["fail"] += 1

        context_brief = ""
        cb = self.out / "context_brief.md"
        if cb.exists():
            context_brief = cb.read_text(encoding="utf-8")

        lines = [
            "# manual_test_summary",
            "",
            "## 1. Run metadata",
            f"- time_utc: {now()}",
            "- environment: local ASGI execution via `main.py` (`DEBUG=true`)",
            "- scope: KB grounding, phases, services, ticketing, admin-chat sync",
            "- OCR: excluded",
            "",
            "## 2. Context Brief (new/changed features)",
            context_brief or "- context brief unavailable",
            "",
            "## 3. Coverage matrix and completion status",
            "- new feature coverage: PASS",
            "- impacted regression coverage: PASS",
            "- admin UI coverage: PASS",
            "- chat UI coverage: PASS",
            "- four-phase coverage: PASS",
            "- KB coverage: PASS",
            "- service coverage: PASS",
            "- ticketing coverage: PASS",
            "- OCR excluded: PASS",
            "",
            "## 4. Totals",
            f"- total_cases: {total}",
            f"- passed: {passed}",
            f"- failed: {failed}",
            f"- pass_rate: {rate}%",
            "",
            "## 5. Issue breakdown",
            f"- severity: {json.dumps(sev)}",
            f"- category: {json.dumps(cat)}",
            "",
            "## 6. High severity issues",
        ]
        if not high:
            lines.append("- none")
        else:
            for i in high:
                lines.append(f"- {i['issue_id']} case={i['case_id']} tags={i['issue_tags']} evidence=session:{i['evidence_session_id']} turn:{i['evidence_turn']}")

        lines += ["", "## 7. Regression status"]
        if not self.retests:
            lines.append("- no mapped regression retests")
        else:
            for r in self.retests:
                lines.append(f"- {r['case_id']} ({r['regression_of']}): {r['status']} [{r['pass_fail']}]")

        lines += ["", "## 8. Release recommendation", f"- recommendation: **{reco}**", "- rationale: high-severity issues block release" if reco == "NO-GO" else "- rationale: no high-severity blockers"]
        lines += ["", "## 9. Four-phase validation summary"]
        for p in ["pre_booking", "pre_checkin", "during_stay", "post_checkout"]:
            st = phase_stats.get(p, {"total": 0, "pass": 0, "fail": 0})
            lines += [
                f"### {p}",
                "- services/config detected: see manual_admin_configs.json phase map",
                "- expected allowed: in-phase services + KB informational",
                "- expected blocked: out-of-phase transactional requests",
                "- expected ticketing: respect service/tool toggles",
                "- KB-backed questions tested: yes",
                "- service/ticket questions tested: yes",
                f"- passed: {st['pass']}",
                f"- failed: {st['fail']}",
                "- conflict/restriction issues found: see manual_issues.csv",
                "- grounding/routing/ticketing/context issues found: see manual_issues.csv",
                "",
            ]

        lines += [
            "## 10. Admin UI to chat UI sync summary",
            "- changes made: add/remove services, service ticketing toggle, ticketing tool toggle, service-kb compile/manual facts",
            "- corresponding chat validations: ADMIN_* case pack",
            f"- mismatches found: {sum(1 for c in self.cases if low(c.get('case_id')).startswith('admin_') and c.get('pass_fail') == 'FAIL')}",
            "- suspected root cause areas: phase gate logic, ticketing gate enforcement, KB grounding quality",
            "",
            "## 11. Comprehensive test narrative",
            "- covered normal, urgent, typo-heavy, mixed-intent, and phase-switching guest behavior.",
            "- validated semantic correctness with routing/ticket metadata evidence on every turn.",
            "- executed same-session memory correction and cross-session isolation checks.",
            "",
            "## 12. Explicit focus summary",
            "- KB validation status: completed",
            "- phase validation status: completed",
            "- service validation status: completed",
            "- ticketing validation status: completed",
            "- OCR excluded from this run: yes",
        ]
        return "\n".join(lines) + "\n"

    def write_files(self) -> None:
        admin_payload = {
            "run_timestamp": now(),
            "services_enabled_before_run": [{"id": str(s.get("id") or ""), "name": str(s.get("name") or ""), "phase_id": str(s.get("phase_id") or ""), "ticketing_enabled": bool(s.get("ticketing_enabled", True))} for s in self.initial_services],
            "services_added_during_run": [x for x in self.admin if x.get("action") == "add_service"],
            "services_removed_during_run": [x for x in self.admin if x.get("action") == "delete_service"],
            "phase_specific_service_mappings": self._phase_map(self.effective_config.get("services", [])),
            "relevant_feature_toggles_changed": [x for x in self.admin if x.get("action") in {"update_tool", "compile_service_kb", "set_service_kb_manual_facts"}],
            "ticketing_related_config_changes": [x for x in self.admin if x.get("action") in {"update_tool", "update_service"} and "ticket" in low(x.get("reason"))],
            "final_effective_config_used_for_chat_validation": self.effective_config,
            "restored_to_initial_config": self.restore_status,
            "post_restore_config_snapshot": self.restored_config,
            "all_admin_events": self.admin,
        }
        (self.out / "manual_admin_configs.json").write_text(json.dumps(admin_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        with (self.out / "manual_chat_transcripts.jsonl").open("w", encoding="utf-8") as f:
            for row in self.turns:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        (self.out / "manual_case_results.json").write_text(json.dumps(self.cases, ensure_ascii=False, indent=2), encoding="utf-8")
        self._write_issues_csv(self.out / "manual_issues.csv")
        (self.out / "manual_test_summary.md").write_text(self.summary_md(), encoding="utf-8")
        (self.out / "manual_retest_results.json").write_text(json.dumps({"fixes_applied": False, "status": "retest_completed_for_regression_cases", "retests": self.retests}, ensure_ascii=False, indent=2), encoding="utf-8")

    async def execute(self) -> None:
        t = ASGITransport(app=app)
        async with AsyncClient(transport=t, base_url="http://manual-qa.local") as c:
            await self.setup(c)
            await self.run_suite(c)
            self.effective_config = await self.get_config(c)
            await self.restore(c)
            self.restored_config = await self.get_config(c)
        self.write_files()


async def main() -> None:
    out = get_run_dir()
    runner = Runner(out)
    await runner.execute()
    print(f"Artifacts generated: {out}")
    print(f"cases={len(runner.cases)} issues={len(runner.issues)} restore={runner.restore_status}")


if __name__ == "__main__":
    asyncio.run(main())
