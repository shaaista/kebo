# Lumira Ticketing Dev Contract (Locked)

Generated on: 2026-02-24

This file captures user-provided dev DB and ticketing details so implementation can proceed without re-discovery.

## 1) Dev Endpoints (Provided)

- `TICKETING_BASE_URL`: `https://dev-reviews.kepsla.com:18080/kepsla-rep-dashboard-svc`
- Create endpoint: `POST /insert/ticket.htm`
- Update endpoint: `PATCH /insert/ticket/{id}.htm` (Lumira code contract)
- Full create URL shared: `https://dev-reviews.kepsla.com:18080/kepsla-rep-dashboard-svc/insert/ticket.htm`
- Handoff URL shared (currently out of scope): `https://dev-reviews.kepsla.com:18080/kepsla-rep-dashboard-svc/insert/transfer.htm`

## 2) Table Sizes (Provided)

- `GHN_PROD_BAK.GHN_OPERATION_TICKETS`: `3519`
- `GHN_PROD_BAK.DEPARTMENT_STAFF_CONFIG`: `136`
- `GHN_PROD_BAK.DEPARTMENT_MASTER`: `28`
- `GHN_PROD_BAK.ORGANIZATION_OUTLETS`: `27`
- `GHN_FEEDBACK_RESULTS.GHN_SURVEY_FMS_RI_ENTITY_MAPPING`: `233`
- `GHN_FF_PROD.GHN_FF_GUEST_INFO`: `939194`
- `GHN_FF_PROD.GHN_FF_WHATSAPP_MESSAGES`: `9777`

## 3) Locked Enum/Value Signals from `GHN_OPERATION_TICKETS`

### Priority distribution
- `MEDIUM`: `1993`
- `LOW`: `653`
- `HIGH`: `608`
- `CRITICAL`: `259`
- `NULL`: `6`

### Phase distribution
- `During Stay`: `2221`
- `Pre Booking`: `850`
- `Pre Checkin`: `358`
- `Booking`: `39`
- `Post Checkout`: `27`
- `NULL`: `24`

### Category distribution
- `request`: `2221`
- `complaint`: `635`
- `NULL`: `585`
- `upsell`: `52`
- empty-string/blank: `12`
- `inquiry`: `6`
- `Food Order`: `2`
- `order`: `2`
- `emergency`: `1`
- `maintenance`: `1`
- `In-Room Dining`: `1`
- `Dining`: `1`

### Status distribution
- `closed`: `2716`
- `open`: `701`
- `cancel`: `77`
- `in_progress`: `22`
- `NULL`: `2`
- `Breached`: `1`

### Ticket source distribution
- `NULL`: `2774`
- `booking_bot`: `386`
- `manual`: `319`
- `whatsapp_bot`: `37`
- `taskly`: `3`

## 4) Missing Field Reality Check (Provided)

- `total_rows`: `3519`
- `sla_due_missing`: `3519`
- `sub_category_missing`: `3412`
- `priority_missing`: `6`
- `category_missing`: `597`
- `phase_missing`: `24`
- `status_missing`: `2`
- `department_alloc_missing`: `0`
- `guest_id_missing`: `0`
- `room_number_missing`: `871`
- `fms_entity_id_missing`: `0`

Implication for bot payload:
- Always include `sla_due_time` and `sub_categorization` keys, but empty values are common in historical data.

## 5) Mapping Coverage Signals (Provided)

From `GHN_SURVEY_FMS_RI_ENTITY_MAPPING`:
- `total_pairs`: `233`
- `distinct_fms`: `232`
- `distinct_ri`: `232`

Ticket mapping join snapshot:
- `tickets_with_fms`: `3520`
- `mapped_tickets`: `2871`
- `unmapped_tickets`: `649`

## 6) Outlet Coverage Signals (Provided)

Top `fms_entity_id` outlet counts:
- `5703`: `14`
- `207`: `7`
- `5614`: `3`
- `0`: `2`
- `5717`: `1`

## 7) Locked Implementation Decisions

1. `priority` normalization target: `CRITICAL|HIGH|MEDIUM|LOW`.
2. `phase` normalization target:
   - `Booking`
   - `Pre Checkin`
   - `During Stay`
   - `Post Checkout`
   - `Pre Booking`
3. `category` normalization target:
   - Preferred: `request|complaint|upsell|inquiry`
   - Preserve unknown category text if not mappable.
4. `ticket_status` for create calls: use `open`.
5. Smart update routing should only consider `status IN ('open', 'in_progress')` (case-insensitive), matching Lumira behavior.

## 8) Source Artifacts Provided by User

Desktop CSV paths:
- `C:\Users\Hp\Desktop\GHN_OPERATION_TICKETS.csv`
- `C:\Users\Hp\Desktop\GHN_OPERATION_TICKETS-1771926184864.csv`
- `C:\Users\Hp\Desktop\GHN_SURVEY_FMS_RI_ENTITY_MAPPING.csv`
- `C:\Users\Hp\Desktop\DEPARTMENT_MASTER.csv`
- `C:\Users\Hp\Desktop\DEPARTMENT_STAFF_CONFIG.csv`
- `C:\Users\Hp\Desktop\_SELECT_DEPARTMENT_ID_ORG_ID_COUNT_AS_total_rows_SUM_MANAGER_1_A.csv`
- `C:\Users\Hp\Desktop\ORGANIZATION_OUTLETS.csv`

Downloads CSV paths:
- `C:\Users\Hp\Downloads\GHN_FF_GUEST_INFO.csv`
- `C:\Users\Hp\Downloads\_SELECT_SOURCE_MSG_TYPE_CATEGORY_SUB_CATEGORY_COUNT_AS_cnt_FROM_.csv`
- `C:\Users\Hp\Downloads\GHN_FF_WHATSAPP_MESSAGES.csv`
- `C:\Users\Hp\Downloads\GHN_OPERATION_TICKETS.csv`

## 9) Still External (Not Derivable from DB)

- Ticket API auth requirement (if any headers/signature are required).
- Real create/update API success and error response samples from dev runtime.
