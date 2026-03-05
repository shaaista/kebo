# Ticketing Integration: How It Works + How To Test (Dev)

Generated on: 2026-02-24

Scope:
- Ticket create + update + smart route (`acknowledge|update|create`)
- Handoff is intentionally out of scope for now.
- Current safe mode: ticket writes are local-file simulated (no external ticket DB/API write).

## 1) Runtime Flow

1. User sends a complaint/request message to `POST /api/chat/message`.
2. `ComplaintHandler` identifies ticket flow and enriches context from DB:
   - departments
   - outlets
   - FMS->RI mapping
   - candidate open tickets
3. Smart router decides:
   - `acknowledge` (same issue already open)
   - `update` (same issue with new details)
   - `create` (new issue)
4. Bot calls ticket API:
   - Local mode (`TICKETING_LOCAL_MODE=true`): writes to local JSON store only.
   - API mode (`TICKETING_LOCAL_MODE=false`):
     - create: `POST {TICKETING_BASE_URL}/insert/ticket.htm`
     - update: `PATCH {TICKETING_BASE_URL}/insert/ticket/{id}.htm`
5. Bot returns ticket response and stores latest ticket in memory metadata.

## 2) Current Normalization Rules

- `phase` normalized to one of:
  - `Booking`, `Pre Checkin`, `During Stay`, `Post Checkout`, `Pre Booking`
- `priority` normalized to:
  - `CRITICAL`, `HIGH`, `MEDIUM`, `LOW`
- `categorization` normalized to:
  - `request`, `complaint`, `upsell`, `inquiry` (unknown values pass through)
- `ticket_status` defaults to `open`
- `source` defaults:
  - `booking_bot` for engage flow
  - `whatsapp_bot` for WhatsApp channel
  - otherwise `manual`

## 3) Prerequisites (Dev)

1. `.env` has:
   - `DATABASE_URL=mysql+aiomysql://...`
   - `TICKETING_LOCAL_MODE=true`
   - `TICKETING_LOCAL_STORE_FILE=./data/ticketing/local_tickets.json`
   - `TICKETING_LOCAL_CSV_FILE=./data/ticketing/local_tickets.csv` (optional; if empty, auto-derives from JSON store path)
   - `TICKETING_BASE_URL=...` (only required if using API mode)
   - `TICKETING_CREATE_PATH=/insert/ticket.htm`
   - `TICKETING_UPDATE_PATH_TEMPLATE=/insert/ticket/{ticket_id}.htm`
2. VPN/network access to DB and ticket API.
3. Redis + OpenAI key are available.

## 4) Start App

```powershell
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

## 5) Test Scenarios

### A) Create ticket (new issue)

Request 1:

```json
POST /api/chat/message
{
  "session_id": "ticket-dev-001",
  "message": "AC is not cooling in room 305, please fix urgently",
  "hotel_code": "DEFAULT",
  "channel": "web",
  "metadata": {
    "flow": "guest_journey",
    "guest_id": 12345,
    "entity_id": 5703,
    "room_number": "305",
    "phase": "During Stay",
    "ticket_source": "whatsapp_bot"
  }
}
```

Expected:
- Bot asks confirmation to create ticket.

Request 2:

```json
POST /api/chat/message
{
  "session_id": "ticket-dev-001",
  "message": "Yes, create ticket",
  "hotel_code": "DEFAULT",
  "channel": "web",
  "metadata": {
    "flow": "guest_journey",
    "guest_id": 12345,
    "entity_id": 5703,
    "room_number": "305",
    "phase": "During Stay"
  }
}
```

Expected:
- Success message with ticket id.
- In local mode ticket id will be `LOCAL-<number>`.
- Response metadata contains:
  - `ticket_created=true`
  - `ticket_id`
  - `ticket_api_status_code`

### B) Smart acknowledge (same issue repeat)

Send near-identical message in same session after ticket create.
Expected:
- Bot acknowledges existing open ticket instead of creating duplicate.
- Metadata includes `ticket_route_decision=acknowledge`.

### C) Smart update (same issue + new detail)

Send:
- "AC still not cooling, please fix by 9 PM"
Expected:
- Bot updates existing open ticket.
- Metadata includes:
  - `ticket_updated=true`
  - `ticket_route_decision=update`

### D) Manual update command

Send:
- "update ticket AC issue still unresolved since 2 hours"
Expected:
- Bot updates latest ticket with manager notes.

### E) Validate local write store

Check `TICKETING_LOCAL_STORE_FILE` after create/update:
- File should exist.
- `tickets` array should contain created ticket record.
- Update flow should append/update `manager_notes` for same `LOCAL-*` ticket.

Check `TICKETING_LOCAL_CSV_FILE` after create:
- CSV should exist with header row.
- Each create should append one row with Lumira-style ticket columns (`priority`, `category`, `phase`, `status`, `sub_category`, `ticket_source`, token/cost fields, etc.).

## 6) What To Verify In Logs/Metadata

1. Create call URL and HTTP status.
2. Update call URL and HTTP status.
3. Parsed ticket id in response.
4. Route decision (`acknowledge|update|create`).
5. DB enrichment applied (`department_id`, `outlet_id`, mapped org where available).

In local mode:
- URL/HTTP checks are replaced by local file write checks.

## 7) Known Non-Blocking Risks

1. If ticket API response keys differ, parser uses tolerant extraction but may miss id in rare formats.
2. If network/VPN is down, DB enrichment and candidate lookup can fail; flow still attempts best-effort create.
