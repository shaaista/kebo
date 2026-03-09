import asyncio
import json
import os
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


class PhaseHistoryRunner:
    def __init__(self) -> None:
        self.root = Path(".")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_dir = self.root / "logs" / "manual_testing" / stamp
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.report_path = self.run_dir / "phase_chat_history.md"
        self.initial_config: dict[str, Any] = {}
        self.admin_events: list[dict[str, Any]] = []
        self.phase_logs: dict[str, list[dict[str, Any]]] = {}
        self.phase_summaries: dict[str, dict[str, Any]] = {}

    async def req(self, c: AsyncClient, method: str, path: str, payload: Any | None = None, params: dict | None = None) -> tuple[int, Any]:
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
        self.admin_events.append({"timestamp": now(), "action": action, "service_id": sid, "reason": reason, "status": s, "response": b, "payload": payload})

    async def configure_phase_services(self, c: AsyncClient) -> None:
        self.initial_config = await self.get_config(c)
        services = [
            {"id": "hotel_enquiry_sales", "name": "Hotel Enquiry & Sales", "type": "service", "description": "Answer hotel questions and guide guests toward booking decisions.", "is_active": True, "ticketing_enabled": True, "phase_id": "pre_booking"},
            {"id": "room_discovery", "name": "Room Discovery", "type": "service", "description": "Help guests compare room types, amenities, and suitability.", "is_active": True, "ticketing_enabled": True, "phase_id": "pre_booking"},
            {"id": "sightseeing_around_hotel", "name": "Sightseeing Around Hotel", "type": "service", "description": "Explore nearby attractions before booking.", "is_active": True, "ticketing_enabled": False, "phase_id": "pre_booking"},
            {"id": "airport_transfer_assist", "name": "Airport Transfer Assist", "type": "service", "description": "Coordinate pre-arrival airport transfer.", "is_active": True, "ticketing_enabled": True, "phase_id": "pre_checkin"},
            {"id": "early_checkin_support", "name": "Early Check-in Support", "type": "service", "description": "Handle early check-in requests and constraints.", "is_active": True, "ticketing_enabled": True, "phase_id": "pre_checkin"},
            {"id": "booking_modification_help", "name": "Booking Modification Help", "type": "service", "description": "Assist booking changes before arrival.", "is_active": True, "ticketing_enabled": True, "phase_id": "pre_checkin"},
            {"id": "housekeeping_request", "name": "Housekeeping Request", "type": "service", "description": "In-stay housekeeping support.", "is_active": True, "ticketing_enabled": True, "phase_id": "during_stay"},
            {"id": "maintenance_support", "name": "Maintenance Support", "type": "service", "description": "In-stay maintenance and engineering issues.", "is_active": True, "ticketing_enabled": True, "phase_id": "during_stay"},
            {"id": "in_room_dining", "name": "In Room Dining", "type": "service", "description": "In-stay in-room dining support.", "is_active": True, "ticketing_enabled": True, "phase_id": "during_stay"},
            {"id": "spa_booking_assist", "name": "Spa Booking Assist", "type": "service", "description": "In-stay spa booking support.", "is_active": True, "ticketing_enabled": True, "phase_id": "during_stay"},
            {"id": "lost_found_desk", "name": "Lost And Found Desk", "type": "service", "description": "Post-checkout lost and found support.", "is_active": True, "ticketing_enabled": True, "phase_id": "post_checkout"},
            {"id": "invoice_billing_support", "name": "Invoice & Billing Support", "type": "service", "description": "Post-checkout billing/invoice support.", "is_active": True, "ticketing_enabled": True, "phase_id": "post_checkout"},
            {"id": "refund_followup", "name": "Refund Follow-up", "type": "service", "description": "Post-checkout refund and charge follow-up.", "is_active": True, "ticketing_enabled": True, "phase_id": "post_checkout"},
        ]
        for svc in services:
            await self.upsert_service(c, svc, "human-style manual run phase setup")

    def eval_turn(self, bot: str, meta: dict[str, Any], expect: dict[str, Any]) -> tuple[str, list[str]]:
        txt = low(bot)
        src = low(meta.get("response_source"))
        phase_gate = "phase_gate" in src or bool(meta.get("phase_service_unavailable"))
        ticket = bool(meta.get("ticket_created"))
        errs: list[str] = []
        if "expect_phase_gate" in expect and phase_gate != bool(expect["expect_phase_gate"]):
            errs.append(f"phase_gate expected={bool(expect['expect_phase_gate'])} actual={phase_gate}")
        if "expect_ticket" in expect and ticket != bool(expect["expect_ticket"]):
            errs.append(f"ticket expected={bool(expect['expect_ticket'])} actual={ticket}")
        kws = [low(x) for x in expect.get("contains_any", []) if str(x).strip()]
        if kws and not any(k in txt for k in kws):
            errs.append(f"missing_any={kws}")
        return ("PASS" if not errs else "FAIL", errs)
    async def run_phase_queries(self, c: AsyncClient) -> None:
        phase_cases: dict[str, list[dict[str, Any]]] = {
            "pre_booking": [
                {"user": "What room types do you have?", "expect": {"expect_phase_gate": False, "contains_any": ["room", "suite"]}},
                {"user": "Price of BLACK CHICKPEA RASSAM?", "expect": {"expect_phase_gate": False, "contains_any": ["415", "black chickpea"]}},
                {"user": "wt r chk-in tyms and airport transfer cost?", "expect": {"expect_phase_gate": False, "contains_any": ["14:00", "2000", "1500"]}},
                {"user": "I want to book room from April 10 to April 12", "expect": {"expect_phase_gate": False}},
                {"user": "Please send housekeeping to room 505 now", "expect": {"expect_phase_gate": True}},
                {"user": "There is cockroach in my room urgent help", "expect": {"contains_any": ["staff", "human", "escal"]}},
                {"user": "what can you help me with right now?", "expect": {"expect_phase_gate": False}},
            ],
            "pre_checkin": [
                {"user": "Need airport pickup from T2 tomorrow", "expect": {"expect_phase_gate": False, "contains_any": ["airport", "1500"]}},
                {"user": "Can I get early checkin at 7am?", "expect": {"expect_phase_gate": False, "contains_any": ["early", "check"]}},
                {"user": "Please modify my booking dates", "expect": {"expect_phase_gate": False}},
                {"user": "I want a new room booking next month", "expect": {"expect_phase_gate": True}},
                {"user": "I left my charger after checkout", "expect": {"expect_phase_gate": True}},
                {"user": "cn u hlp me with arprt trnsfr n earl chek in", "expect": {"expect_phase_gate": False}},
                {"user": "what can you help me with right now?", "expect": {"expect_phase_gate": False, "contains_any": ["airport", "check"]}},
            ],
            "during_stay": [
                {"user": "Need room cleaning and fresh towels in 612", "expect": {"expect_phase_gate": False}},
                {"user": "My AC is leaking please create ticket", "expect": {"expect_phase_gate": False, "expect_ticket": True}},
                {"user": "What is price of BLACK CHICKPEA RASSAM?", "expect": {"expect_phase_gate": False, "contains_any": ["415", "black chickpea"]}},
                {"user": "Book a spa session for 7 pm", "expect": {"expect_phase_gate": False, "contains_any": ["spa"]}},
                {"user": "I feel very sick, doctor on call fee?", "expect": {"expect_phase_gate": False, "contains_any": ["2500", "doctor"]}},
                {"user": "I want to book a room for next month", "expect": {"expect_phase_gate": True}},
                {"user": "what can you help me with right now?", "expect": {"expect_phase_gate": False}},
            ],
            "post_checkout": [
                {"user": "I forgot my wallet in room after checkout", "expect": {"expect_phase_gate": False, "contains_any": ["lost", "found", "wallet"]}},
                {"user": "Please resend my invoice", "expect": {"expect_phase_gate": False, "contains_any": ["invoice", "billing"]}},
                {"user": "I need refund for wrong charge", "expect": {"expect_phase_gate": False, "contains_any": ["refund", "charge"]}},
                {"user": "I need housekeeping now", "expect": {"expect_phase_gate": True}},
                {"user": "Can I book room for next week", "expect": {"expect_phase_gate": True}},
                {"user": "chrg dspute urgent, need human", "expect": {"expect_phase_gate": False, "contains_any": ["staff", "human", "ticket", "escal"]}},
                {"user": "what can you help me with right now?", "expect": {"expect_phase_gate": False}},
            ],
        }

        for phase, cases in phase_cases.items():
            session_id = f"manual_ui_{phase}_{datetime.now().strftime('%H%M%S')}"
            self.phase_logs[phase] = []
            worked = 0
            failed = 0
            fail_notes: list[str] = []

            for idx, case in enumerate(cases, start=1):
                s, body = await self.req(
                    c,
                    "POST",
                    "/api/chat/message",
                    {
                        "session_id": session_id,
                        "message": case["user"],
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
                verdict, errs = self.eval_turn(bot, meta, case.get("expect") or {})
                if s != 200:
                    verdict = "FAIL"
                    errs.append(f"http_status={s}")

                if verdict == "PASS":
                    worked += 1
                else:
                    failed += 1
                    fail_notes.append(f"turn {idx}: {', '.join(errs)}")

                self.phase_logs[phase].append(
                    {
                        "turn": idx,
                        "session_id": session_id,
                        "user": case["user"],
                        "bot": bot,
                        "intent": intent,
                        "state": state,
                        "response_source": str(meta.get("response_source") or ""),
                        "ticket_created": bool(meta.get("ticket_created")),
                        "verdict": verdict,
                        "errors": errs,
                    }
                )

            self.phase_summaries[phase] = {
                "total": len(cases),
                "worked": worked,
                "failed": failed,
                "failed_notes": fail_notes,
            }
    def build_report(self, final_services: list[dict[str, Any]]) -> str:
        by_phase: dict[str, list[str]] = {"pre_booking": [], "pre_checkin": [], "during_stay": [], "post_checkout": []}
        for svc in final_services:
            p = low(svc.get("phase_id"))
            if p in by_phase and bool(svc.get("is_active", True)):
                by_phase[p].append(f"{svc.get('name')} (ticketing={'on' if bool(svc.get('ticketing_enabled', True)) else 'off'})")

        lines: list[str] = []
        lines.append("# Manual UI-Like Phase Chat History")
        lines.append("")
        lines.append(f"- Run timestamp (UTC): {now()}")
        lines.append(f"- Run folder: `{self.run_dir}`")
        lines.append("- Method: Admin/UI-equivalent API calls (`/admin/api/config/*`, `/api/chat/message` with `metadata.phase`) to simulate manual UI behavior")
        lines.append("")
        lines.append("## Admin Configuration Applied")
        lines.append("Services added/updated for this run:")
        for phase in ["pre_booking", "pre_checkin", "during_stay", "post_checkout"]:
            svc_list = by_phase.get(phase, [])
            lines.append(f"- {phase}: {', '.join(svc_list) if svc_list else '(none)'}")
        lines.append("")
        lines.append("Admin actions (chronological):")
        for evt in self.admin_events:
            sid = evt.get("service_id") or evt.get("tool_id") or "-"
            lines.append(f"- {evt.get('timestamp')} | {evt.get('action')} | target={sid} | status={evt.get('status')} | reason={evt.get('reason')}")

        for phase in ["pre_booking", "pre_checkin", "during_stay", "post_checkout"]:
            lines.append("")
            lines.append(f"## Phase: {phase}")
            lines.append("")
            logs = self.phase_logs.get(phase, [])
            for row in logs:
                lines.append(f"### Turn {row['turn']}")
                lines.append(f"- User: {row['user']}")
                lines.append(f"- Bot: {row['bot']}")
                lines.append(f"- Intent/State: {row['intent']} / {row['state']}")
                lines.append(f"- Routing source: {row['response_source']}")
                lines.append(f"- Ticket created: {row['ticket_created']}")
                lines.append(f"- Verdict: {row['verdict']}")
                if row["errors"]:
                    lines.append(f"- Notes: {', '.join(row['errors'])}")
                lines.append("")

            summary = self.phase_summaries.get(phase, {"total": 0, "worked": 0, "failed": 0, "failed_notes": []})
            lines.append(f"### {phase} Summary")
            lines.append(f"- Total turns: {summary['total']}")
            lines.append(f"- Worked fine: {summary['worked']}")
            lines.append(f"- Did not work: {summary['failed']}")
            if summary["failed_notes"]:
                lines.append("- What did not work:")
                for note in summary["failed_notes"]:
                    lines.append(f"  - {note}")
            else:
                lines.append("- What did not work: none")

        lines.append("")
        lines.append("## Run Cleanup")
        lines.append(f"- Initial config restored: {self.restore_status}")
        return "\n".join(lines) + "\n"

    async def restore_config(self, c: AsyncClient) -> None:
        if not self.initial_config:
            self.restore_status = "skipped_no_initial_config"
            return
        s, _ = await self.req(c, "PUT", "/admin/api/config", self.initial_config)
        self.restore_status = "success" if s == 200 else f"failed_status_{s}"

    async def execute(self) -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://manual-ui.local") as c:
            await self.configure_phase_services(c)
            await self.run_phase_queries(c)
            final_services = await self.get_services(c)
            await self.restore_config(c)
        report = self.build_report(final_services)
        self.report_path.write_text(report, encoding="utf-8")
        print(f"Report generated: {self.report_path}")


async def main() -> None:
    runner = PhaseHistoryRunner()
    await runner.execute()


if __name__ == "__main__":
    asyncio.run(main())
