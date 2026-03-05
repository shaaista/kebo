import pytest

from services.ticketing_router_service import TicketingRouterService


@pytest.mark.asyncio
async def test_decide_returns_create_when_no_candidates():
    service = TicketingRouterService()
    decision = await service.decide(
        conversation="User: hello",
        latest_user_message="AC is not cooling",
        candidates=[],
    )
    assert decision.decision == "create"


def test_heuristic_acknowledges_same_issue():
    service = TicketingRouterService()
    decision = service._heuristic_decide(
        latest_user_message="AC not cooling in room 305",
        candidates=[
            {
                "id": "11",
                "issue": "AC not cooling in room 305",
                "manager_notes": "",
            }
        ],
    )
    assert decision.decision == "acknowledge"


def test_heuristic_updates_when_context_changes():
    service = TicketingRouterService()
    decision = service._heuristic_decide(
        latest_user_message="AC not cooling in room 305, please fix by 9 PM",
        candidates=[
            {
                "id": "11",
                "issue": "AC not cooling in room 305",
                "manager_notes": "",
            }
        ],
    )
    assert decision.decision == "update"
    assert decision.update_ticket_id == "11"
    assert "9 pm" in decision.manager_notes.lower()

