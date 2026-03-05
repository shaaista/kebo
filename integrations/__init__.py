"""External integrations (WhatsApp, Kepsla API, legacy Lumira DB adapters)."""

from integrations.lumira_ticketing_repository import (
    LumiraTicketingRepository,
    lumira_ticketing_repository,
)

__all__ = [
    "LumiraTicketingRepository",
    "lumira_ticketing_repository",
]
