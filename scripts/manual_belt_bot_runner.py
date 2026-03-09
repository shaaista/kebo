import asyncio
import json
import os
import re
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from httpx import ASGITransport, AsyncClient

os.environ.setdefault("DEBUG", "true")
os.environ.setdefault("PYTHONPATH", ".")

from main import app  # noqa: E402


def now() -> str:
    return datetime.now(UTC).isoformat()


def low(v: Any) -> str:
    return str(v or "").strip().lower()


class BeltBotRunner:
    def __init__(self) -> None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_dir = Path("logs") / "manual_testing" / stamp
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.report_path = self.run_dir / "belt_bot_summary.md"
        self.raw_path = self.run_dir / "belt_bot_transcript.json"
        self.initial_config: dict[str, Any] = {}
        self.restore_status = "not_attempted"
        self.admin_actions: list[dict[str, Any]] = []
        self.turns: list[dict[str, Any]] = []
        self.phase_stats: dict[str, dict[str, Any]] = {}

    async def req(
        self,
        c: AsyncClient,
        method: str,
        path: str,
        payload: Any | None = None,
        params: dict[str, Any] | None = None,
    ) -> tuple[int, Any]:
        r = await c.request(method, path, json=payload, params=params, timeout=180.0)
        try:
            body = r.json()
        except Exception:
            body = {"raw_text": r.text}
        return r.status_code, body

    async def get_config(self, c: AsyncClient) -> dict[str, Any]:
        s, b = await self.req(c, "GET", "/admin/api/config")
        return dict(b) if s == 200 and isinstance(b, dict) else {}

    async def get_services(self, c: AsyncClient) -> list[dict[str, Any]]:
        s, b = await self.req(c, "GET", "/admin/api/config/services")
        return [dict(x) for x in b] if s == 200 and isinstance(b, list) else []

    async def upsert_service(self, c: AsyncClient, payload: dict[str, Any], reason: str) -> None:
        sid = str(payload.get("id") or "").strip()
        cur = await self.get_services(c)
        exists = any(low(s.get("id")) == low(sid) for s in cur)
        if exists:
            s, b = await self.req(c, "PUT", f"/admin/api/config/services/{sid}", payload)
            action = "update_service"
        else:
            s, b = await self.req(c, "POST", "/admin/api/config/services", payload)
            action = "add_service"
        self.admin_actions.append(
            {
                "timestamp": now(),
                "action": action,
                "service_id": sid,
                "reason": reason,
                "status": s,
                "response": b,
            }
        )

    async def configure_services(self, c: AsyncClient) -> None:
        self.initial_config = await self.get_config(c)
        targets = [
            {"id": "hotel_enquiry_sales", "name": "Hotel Enquiry & Sales", "type": "service", "description": "Answer hotel questions and guide guests before booking.", "is_active": True, "ticketing_enabled": True, "phase_id": "pre_booking"},
            {"id": "room_discovery", "name": "Room Discovery", "type": "service", "description": "Compare room types and suitability.", "is_active": True, "ticketing_enabled": True, "phase_id": "pre_booking"},
            {"id": "sightseeing_around_hotel", "name": "Sightseeing Around Hotel", "type": "service", "description": "Nearby attractions guidance.", "is_active": True, "ticketing_enabled": False, "phase_id": "pre_booking"},
            {"id": "airport_transfer_assist", "name": "Airport Transfer Assist", "type": "service", "description": "Pre-arrival transfer help.", "is_active": True, "ticketing_enabled": True, "phase_id": "pre_checkin"},
            {"id": "early_checkin_support", "name": "Early Check-in Support", "type": "service", "description": "Early check-in handling.", "is_active": True, "ticketing_enabled": True, "phase_id": "pre_checkin"},
            {"id": "booking_modification_help", "name": "Booking Modification Help", "type": "service", "description": "Booking modification support.", "is_active": True, "ticketing_enabled": True, "phase_id": "pre_checkin"},
            {"id": "housekeeping_request", "name": "Housekeeping Request", "type": "service", "description": "Cleaning and linen support.", "is_active": True, "ticketing_enabled": True, "phase_id": "during_stay"},
            {"id": "maintenance_support", "name": "Maintenance Support", "type": "service", "description": "Engineering and maintenance support.", "is_active": True, "ticketing_enabled": True, "phase_id": "during_stay"},
            {"id": "in_room_dining", "name": "In Room Dining", "type": "service", "description": "In-room dining support.", "is_active": True, "ticketing_enabled": True, "phase_id": "during_stay"},
            {"id": "spa_booking_assist", "name": "Spa Booking Assist", "type": "service", "description": "Spa booking support.", "is_active": True, "ticketing_enabled": True, "phase_id": "during_stay"},
            {"id": "lost_found_desk", "name": "Lost And Found Desk", "type": "service", "description": "Lost and found support.", "is_active": True, "ticketing_enabled": True, "phase_id": "post_checkout"},
            {"id": "invoice_billing_support", "name": "Invoice & Billing Support", "type": "service", "description": "Billing and invoice clarifications.", "is_active": True, "ticketing_enabled": True, "phase_id": "post_checkout"},
            {"id": "refund_followup", "name": "Refund Follow-up", "type": "service", "description": "Refund and charge dispute follow-up.", "is_active": True, "ticketing_enabled": True, "phase_id": "post_checkout"},
        ]
        for svc in targets:
            await self.upsert_service(c, svc, "belt-bot run setup")

    def evaluate(self, response_text: str, metadata: dict[str, Any], expect: dict[str, Any]) -> tuple[str, list[str]]:
        txt = low(response_text)
        source = low(metadata.get("response_source"))
        phase_gate = "phase_gate" in source or bool(metadata.get("phase_service_unavailable"))
        ticket = bool(metadata.get("ticket_created"))
        errors: list[str] = []

        if "expect_phase_gate" in expect and phase_gate != bool(expect["expect_phase_gate"]):
            errors.append(f"phase_gate expected={bool(expect['expect_phase_gate'])} actual={phase_gate}")
        if "expect_ticket" in expect and ticket != bool(expect["expect_ticket"]):
            errors.append(f"ticket expected={bool(expect['expect_ticket'])} actual={ticket}")

        any_words = [low(x) for x in expect.get("contains_any", []) if str(x).strip()]
        if any_words and not any(w in txt for w in any_words):
            errors.append(f"missing_any={any_words}")

        not_words = [low(x) for x in expect.get("not_contains", []) if str(x).strip()]
        present = [w for w in not_words if w in txt]
        if present:
            errors.append(f"unexpected_present={present}")

        if expect.get("expect_human_or_ticket"):
            mentions_human = any(w in txt for w in ["staff", "human", "agent", "escalat", "front desk", "team"])
            if not mentions_human and not ticket:
                errors.append("expected_human_or_ticket_path")

        if expect.get("expect_no_obvious_hallucinated_price"):
            price_tokens = re.findall(r"(?:rs|inr|₹)\s*\d{2,5}", txt)
            if price_tokens and not any(k in txt for k in ["2000", "1500", "2500", "415", "295", "350", "665", "715"]):
                errors.append("possible_price_hallucination")

        return ("PASS" if not errors else "FAIL", errors)
    async def run_phase(self, c: AsyncClient, phase: str, prompts: list[dict[str, Any]]) -> None:
        session_id = f"belt_{phase}_{datetime.now().strftime('%H%M%S')}"
        self.phase_stats[phase] = {"total": 0, "pass": 0, "fail": 0, "fail_notes": []}

        for idx, item in enumerate(prompts, start=1):
            user_text = str(item.get("user") or "")
            expect = dict(item.get("expect") or {})
            s, body = await self.req(
                c,
                "POST",
                "/api/chat/message",
                {
                    "session_id": session_id,
                    "message": user_text,
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
            metadata = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
            verdict, notes = self.evaluate(bot, metadata, expect)
            if s != 200:
                verdict = "FAIL"
                notes.append(f"http_status={s}")

            self.phase_stats[phase]["total"] += 1
            if verdict == "PASS":
                self.phase_stats[phase]["pass"] += 1
            else:
                self.phase_stats[phase]["fail"] += 1
                self.phase_stats[phase]["fail_notes"].append(f"turn {idx}: {', '.join(notes)}")

            self.turns.append(
                {
                    "timestamp": now(),
                    "phase": phase,
                    "session_id": session_id,
                    "turn": idx,
                    "user": user_text,
                    "bot": bot,
                    "intent": intent,
                    "state": state,
                    "response_source": str(metadata.get("response_source") or ""),
                    "ticket_created": bool(metadata.get("ticket_created")),
                    "trace_id": str(metadata.get("trace_id") or ""),
                    "verdict": verdict,
                    "notes": notes,
                }
            )

    def scenario(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "pre_booking": [
                {"user": "Hey, planning a surprise anniversary trip. Which room gives max privacy?", "expect": {"expect_phase_gate": False, "contains_any": ["room", "suite"]}},
                {"user": "Compare Lux Suite vs Reserve Suite quickly.", "expect": {"expect_phase_gate": False, "contains_any": ["lux", "reserve"]}},
                {"user": "Any sugar-free or non-dairy dessert at Aviator?", "expect": {"expect_phase_gate": False, "contains_any": ["dairy", "vegan", "dessert", "aviator"]}},
                {"user": "If my flight lands 3 AM can I check in early?", "expect": {"expect_phase_gate": False, "contains_any": ["early", "check"]}},
                {"user": "Price of BLACK CHICKPEA RASSAM and one fish option?", "expect": {"expect_phase_gate": False, "contains_any": ["415", "black chickpea"], "expect_no_obvious_hallucinated_price": True}},
                {"user": "Book room April 21 to April 23 for 2 adults", "expect": {"expect_phase_gate": False}},
                {"user": "Actually make it 3 adults and quiet room", "expect": {"expect_phase_gate": False}},
                {"user": "Need housekeeping now in room 203", "expect": {"expect_phase_gate": True}},
                {"user": "There is a cockroach in my room urgent, I need human right now", "expect": {"expect_human_or_ticket": True}},
                {"user": "what can u help me with rn lol", "expect": {"expect_phase_gate": False}},
                {"user": "cn u d0 arprt trnsfr + room + spa in one go?", "expect": {"expect_phase_gate": False}},
                {"user": "Do you have anything weirdly cool in rooms?", "expect": {"expect_phase_gate": False, "contains_any": ["smart", "laundry", "amenit", "room"]}},
            ],
            "pre_checkin": [
                {"user": "Booking confirmed. Need airport pickup from T2 tomorrow evening.", "expect": {"expect_phase_gate": False, "contains_any": ["airport", "t2", "1500"]}},
                {"user": "Can I request 7am early check-in?", "expect": {"expect_phase_gate": False, "contains_any": ["early", "check"]}},
                {"user": "Please modify stay by +1 night", "expect": {"expect_phase_gate": False}},
                {"user": "I want to explore sightseeing tours before arrival", "expect": {"expect_phase_gate": True}},
                {"user": "also if room smells weird what do I do when I arrive?", "expect": {"expect_phase_gate": False}},
                {"user": "new booking for my friend next month", "expect": {"expect_phase_gate": True}},
                {"user": "I left item after checkout last time", "expect": {"expect_phase_gate": False}},
                {"user": "cn u hlp me w arprt + early chk + modify all now", "expect": {"expect_phase_gate": False}},
                {"user": "Do I need docs at checkin?", "expect": {"expect_phase_gate": False}},
                {"user": "What can you help me with right now exactly?", "expect": {"expect_phase_gate": False}},
                {"user": "If I get delayed after midnight who should I notify?", "expect": {"expect_phase_gate": False, "expect_human_or_ticket": True}},
                {"user": "Can you create a support ticket for my transfer anyway?", "expect": {"expect_human_or_ticket": True}},
            ],
            "during_stay": [
                {"user": "Room 612 needs deep cleaning and fresh towels.", "expect": {"expect_phase_gate": False}},
                {"user": "AC leaking badly, create urgent maintenance ticket.", "expect": {"expect_phase_gate": False, "expect_ticket": True}},
                {"user": "Need in-room dining: something vegetarian under 500.", "expect": {"expect_phase_gate": False, "contains_any": ["vegetarian", "500", "menu", "dish"]}},
                {"user": "Book spa at 7:30 pm today.", "expect": {"expect_phase_gate": False, "contains_any": ["spa"]}},
                {"user": "I feel dizzy, doctor on call details and fee?", "expect": {"expect_phase_gate": False, "contains_any": ["doctor", "2500"]}},
                {"user": "Can you book a new room for next month for my cousin", "expect": {"expect_phase_gate": True}},
                {"user": "My order delayed 50 mins, escalate to manager.", "expect": {"expect_human_or_ticket": True}},
                {"user": "Do you have pool rule for kids after 7pm?", "expect": {"expect_phase_gate": False, "contains_any": ["children", "7"]}},
                {"user": "I changed my mind: no spa, just housekeeping twice daily.", "expect": {"expect_phase_gate": False}},
                {"user": "Can you remember my room is 612 and call me Sam?", "expect": {"expect_phase_gate": False}},
                {"user": "What room did I just mention?", "expect": {"contains_any": ["612"]}},
                {"user": "What can you do for me right now?", "expect": {"expect_phase_gate": False}},
            ],
            "post_checkout": [
                {"user": "I checked out yesterday and forgot my wallet.", "expect": {"expect_phase_gate": False, "contains_any": ["lost", "wallet", "found"]}},
                {"user": "Please resend final invoice to my email.", "expect": {"expect_phase_gate": False, "contains_any": ["invoice", "billing"]}},
                {"user": "Wrong minibar charge, need refund.", "expect": {"expect_phase_gate": False, "contains_any": ["refund", "charge"]}},
                {"user": "Need housekeeping in room now", "expect": {"expect_phase_gate": True}},
                {"user": "Book room for next month", "expect": {"expect_phase_gate": True}},
                {"user": "charge dispute urgent, get me human", "expect": {"expect_human_or_ticket": True}},
                {"user": "Can you courier my forgotten charger to Bangalore?", "expect": {"expect_phase_gate": False, "contains_any": ["lost", "found", "courier", "team"]}},
                {"user": "I also need GST invoice correction.", "expect": {"expect_phase_gate": False, "contains_any": ["invoice", "gst", "billing"]}},
                {"user": "Can you waive my charge right now?", "expect": {"expect_phase_gate": False, "expect_human_or_ticket": True}},
                {"user": "Do you keep my card details after checkout?", "expect": {"expect_phase_gate": False}},
                {"user": "What all can you help with right now?", "expect": {"expect_phase_gate": False}},
                {"user": "thx bye but one last thing: my friend needs pre-booking help", "expect": {"expect_phase_gate": True}},
            ],
        }
    def build_report(self) -> str:
        lines: list[str] = []
        lines.append("# Belt Bot Manual Test Report")
        lines.append("")
        lines.append(f"- Run timestamp (UTC): {now()}")
        lines.append(f"- Run folder: `{self.run_dir}`")
        lines.append("- Scope: long continuous conversations per phase, out-of-box questions, mixed intent, adversarial and typo variants")
        lines.append("- OCR: excluded")
        lines.append("")
        lines.append("## Admin Setup")
        if self.admin_actions:
            for evt in self.admin_actions:
                lines.append(f"- {evt['timestamp']} | {evt['action']} | {evt['service_id']} | status={evt['status']} | {evt['reason']}")
        else:
            lines.append("- No admin changes applied")

        lines.append("")
        lines.append("## Full Chat History")
        grouped: dict[str, list[dict[str, Any]]] = {"pre_booking": [], "pre_checkin": [], "during_stay": [], "post_checkout": []}
        for t in self.turns:
            grouped.setdefault(t["phase"], []).append(t)

        for phase in ["pre_booking", "pre_checkin", "during_stay", "post_checkout"]:
            lines.append("")
            lines.append(f"### Phase: {phase}")
            for t in grouped.get(phase, []):
                lines.append(f"- Turn {t['turn']}")
                lines.append(f"  - User: {t['user']}")
                lines.append(f"  - Bot: {t['bot']}")
                lines.append(f"  - Intent/State: {t['intent']} / {t['state']}")
                lines.append(f"  - Source: {t['response_source']}")
                lines.append(f"  - Ticket created: {t['ticket_created']}")
                lines.append(f"  - Verdict: {t['verdict']}")
                if t["notes"]:
                    lines.append(f"  - Notes: {', '.join(t['notes'])}")

            st = self.phase_stats.get(phase, {"total": 0, "pass": 0, "fail": 0, "fail_notes": []})
            lines.append(f"- Phase summary: total={st['total']} pass={st['pass']} fail={st['fail']}")
            if st["fail_notes"]:
                lines.append("- What didn't work:")
                for n in st["fail_notes"]:
                    lines.append(f"  - {n}")
            else:
                lines.append("- What didn't work: none")

        total = len(self.turns)
        passed = sum(1 for t in self.turns if t["verdict"] == "PASS")
        failed = total - passed

        lines.append("")
        lines.append("## Summary")
        lines.append(f"- Total turns: {total}")
        lines.append(f"- Worked well: {passed}")
        lines.append(f"- Not working: {failed}")

        good_signals = []
        bad_signals = []

        if any("full_kb_llm" in low(t["response_source"]) and t["verdict"] == "PASS" for t in self.turns):
            good_signals.append("KB-grounded answers were often correct for room/menu/policy details.")
        if any(t["ticket_created"] for t in self.turns):
            good_signals.append("Ticketing path triggers in some escalation scenarios.")
        if any(t["phase"] == "post_checkout" and t["verdict"] == "PASS" for t in self.turns):
            good_signals.append("Post-checkout flows (lost-found/billing/refund) were mostly coherent.")

        phase_gate_fail = [t for t in self.turns if any("phase_gate" in n for n in t["notes"])]
        if phase_gate_fail:
            bad_signals.append("Phase restriction behavior is inconsistent (false blocks and false allows).")
        ticket_fail = [t for t in self.turns if any("ticket expected" in n for n in t["notes"])]
        if ticket_fail:
            bad_signals.append("Ticket creation is inconsistent when explicitly requested or expected.")
        urgent_fail = [t for t in self.turns if "urgent" in low(t["user"]) and t["verdict"] == "FAIL"]
        if urgent_fail:
            bad_signals.append("Urgent/escalation prompts still fail to route reliably to human-escalation behavior.")
        memory_fail = [t for t in self.turns if "what room did i just mention" in low(t["user"]) and t["verdict"] == "FAIL"]
        if memory_fail:
            bad_signals.append("Short-term memory continuity is unstable inside long conversations.")

        lines.append("- Working well:")
        if good_signals:
            for g in good_signals:
                lines.append(f"  - {g}")
        else:
            lines.append("  - No strong positives observed")

        lines.append("- Not working well:")
        if bad_signals:
            for b in bad_signals:
                lines.append(f"  - {b}")
        else:
            lines.append("  - No major issues observed")

        lines.append("")
        lines.append("## Cleanup")
        lines.append(f"- Initial config restored: {self.restore_status}")
        return "\n".join(lines) + "\n"

    async def restore_config(self, c: AsyncClient) -> None:
        if not self.initial_config:
            self.restore_status = "skipped_no_initial_config"
            return
        s, _ = await self.req(c, "PUT", "/admin/api/config", deepcopy(self.initial_config))
        self.restore_status = "success" if s == 200 else f"failed_status_{s}"

    async def execute(self) -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://belt-bot.local") as c:
            await self.configure_services(c)
            scenario = self.scenario()
            for phase in ["pre_booking", "pre_checkin", "during_stay", "post_checkout"]:
                await self.run_phase(c, phase, scenario[phase])
            await self.restore_config(c)

        self.raw_path.write_text(json.dumps({"turns": self.turns, "phase_stats": self.phase_stats, "admin_actions": self.admin_actions, "restore_status": self.restore_status}, ensure_ascii=False, indent=2), encoding="utf-8")
        self.report_path.write_text(self.build_report(), encoding="utf-8")
        print(f"Report: {self.report_path}")
        print(f"Transcript: {self.raw_path}")


async def main() -> None:
    runner = BeltBotRunner()
    await runner.execute()


if __name__ == "__main__":
    asyncio.run(main())
