# DB Migration Pack: 2026-02-11 (Efficiency + Multi-tenant Hygiene)

This migration pack adds missing conversation persistence columns, adds query-performance indexes, and consolidates data to a single canonical business row to avoid split-tenant behavior.

## Scope

- Add columns:
  - `new_bot_conversations.pending_action`
  - `new_bot_conversations.pending_data`
  - `new_bot_conversations.channel`
  - `new_bot_messages.channel`
- Add indexes:
  - `idx_messages_conv_created` on `new_bot_messages(conversation_id, created_at, id)`
  - `idx_conversations_hotel_state` on `new_bot_conversations(hotel_id, state)`
  - `idx_orders_rest_status_created` on `new_bot_orders(restaurant_id, status, created_at)`
- Consolidate all `hotel_id != canonical` rows into a canonical hotel:
  - `business_config`, `capabilities`, `intents` via upsert merge
  - `restaurants`, `guests`, `conversations` via hotel_id update (after conflict checks)
  - mark non-canonical hotels inactive
  - canonical hotel set active and code forced to `DEFAULT`

## Files

- `00_precheck.sql`: non-destructive checks, conflicts, and row distribution.
- `01_backup.sql`: creates backup snapshot tables (`bak_20260211_*`).
- `02_up.sql`: applies schema + data migration.
- `03_verify.sql`: post-migration validation.
- `04_down.sql`: rollback using backup snapshot + schema revert.

## Run Order

1. `00_precheck.sql`
2. `01_backup.sql`
3. `02_up.sql`
4. `03_verify.sql`

Rollback (if needed):

5. `04_down.sql`

## How canonical hotel is selected

`02_up.sql` uses:

```sql
COALESCE(
  (SELECT id FROM new_bot_hotels WHERE code = 'DEFAULT' ORDER BY id LIMIT 1),
  (SELECT MIN(id) FROM new_bot_hotels)
)
```

If you want a specific canonical row, edit `@canonical_hotel_id` in `02_up.sql` before running.

## Safety Notes

- `02_up.sql` aborts before data movement if it detects:
  - duplicate restaurant codes between source and canonical hotels
  - duplicate guest phone numbers between source and canonical hotels
- DDL in MySQL is not fully transactional. Backups are mandatory.
- `04_down.sql` assumes `bak_20260211_*` tables exist and were created by `01_backup.sql`.

## Post migration app work (separate)

Schema is ready, but app code should be updated to persist/reload:
- `pending_action`
- `pending_data`
- `channel`

from DB conversation/message tables.
