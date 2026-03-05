# RAG Human Test Cases (Knowledge-Only Validation)

Use this checklist after uploading KB files and running reindex.

## Pre-check
1. Open Admin -> `RAG & Agents`.
2. Confirm tenant in UI is the same business ID.
3. Upload KB file(s).
4. Click `Run Reindex` (sync or async).
5. Verify job status = `completed`.

## Core Retrieval Cases
1. Query: `List all food/menu available`
   Expected: Mentions menu sources/sections (Kadak, Aviator, IRD sections) from KB.

2. Query: `Show in room dining menu with prices`
   Expected: Returns IRD section names and item+price lines.

3. Query: `Show IRD breakfast section`
   Expected: Returns breakfast items from IRD section only.

4. Query: `Show IRD midnight selections`
   Expected: Returns midnight IRD items; no “not found”.

5. Query: `What are check-in and check-out times?`
   Expected: Returns values from KB (14:00 / 11:00 if present in source).

6. Query: `Spa timings?`
   Expected: Returns spa timing from KB.

## Robustness Cases
1. Query: `i need food show us your menus`
   Expected: Still returns menu-related answer from KB.

2. Query: `show me your in room dining menu so that i can order`
   Expected: Returns IRD sections/items, not generic “contact staff”.

3. Query: `what are options for food to my room`
   Expected: Gives KB-grounded options first. If order flow starts, it should still reference KB-backed options.

## Pass Criteria
1. No answer should say “not found / not sure” when matching data exists in uploaded KB.
2. Answers should include at least one relevant section/item from KB for menu queries.
3. For timing/policy questions, values must match KB text.

## Failure Log Template
Record each failure:
- Query:
- Bot response:
- Expected response:
- RAG source list shown (if available):
- Tenant ID used:
- Reindex job ID/time:
