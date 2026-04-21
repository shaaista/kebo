# Plan: Connect Scraper Assets to Shared MySQL DB

## Context

- **Main DB:** `GHN_PROD_BAK` on `172.16.5.32` (MySQL, credentials in `.env`)
- **Scraper currently forced to SQLite** via override in `main.py` (lines 244–248)
- **Goal:** Everything in the same MySQL DB — scraper writes image URLs + KB file paths there after each job

---

## Step 1 — Remove SQLite override in `main.py`

**File:** `main.py` (lines 244–248)

Delete the 4 lines that override `DATABASE_URL` to SQLite. The scraper will then inherit:

```
DATABASE_URL=mysql+aiomysql://root:zapcom123@172.16.5.32:3306/GHN_PROD_BAK
```

from `.env` automatically.

---

## Step 2 — Add `new_bot_kb_assets` table to main DB

**File:** `models/database.py`

Add `KBAsset` ORM model after the existing `KBFile` model. Also add `Hotel.kb_assets` relationship and a migration entry in `init_db()`.

### Table: `new_bot_kb_assets`

| Column | Type | Notes |
|---|---|---|
| `id` | INT PK AUTO_INCREMENT | |
| `hotel_id` | INT nullable FK → `new_bot_hotels` | Nullable — scraper runs before hotel is linked |
| `job_id` | VARCHAR(36) | Scrape job UUID |
| `asset_type` | VARCHAR(20) | `'image'` or `'kb_file'` |
| `property_name` | VARCHAR(255) | e.g. "Mumbai Orchid" |
| `source_url` | TEXT | Page the asset was scraped from |
| `url` | TEXT | Original image `src` URL (images only) |
| `local_path` | TEXT | Absolute disk path to downloaded file |
| `file_name` | VARCHAR(500) | Filename on disk |
| `title` | VARCHAR(500) | Image alt/title or KB file label |
| `size_kb` | FLOAT | File size in KB |
| `created_at` | DATETIME | Auto-set on insert |

---

## Step 3 — Add same model to scraper's `models/job.py`

**File:** `Kepsla-hotal-kb-scraper-main/models/job.py`

Add identical `KBAsset` model using the scraper's own `Base`. Since both apps point to the same MySQL DB, `create_all()` creates the table once — no conflict.

---

## Step 4 — Wire `image_downloader.py` to save image rows

**File:** `Kepsla-hotal-kb-scraper-main/scraper/image_downloader.py`

After `_create_excel_catalog(...)` completes, call a new async helper:

```python
await save_image_assets_to_db(job_id, property_name, source_url, downloaded_data)
```

This bulk-inserts one `KBAsset` row per successfully downloaded image with `asset_type='image'`.

---

## Step 5 — Wire `file_packager.py` to save KB file rows

**File:** `Kepsla-hotal-kb-scraper-main/generator/file_packager.py`

After each KB `.txt` file is written to disk, call:

```python
await save_kb_asset_to_db(job_id, property_name, file_path, source_url, size_kb)
```

Inserts one `KBAsset` row with `asset_type='kb_file'`.

---

## Result After Execution

- `new_bot_kb_assets` table in MySQL containing every image URL and KB file path per scrape job
- Scraper no longer needs SQLite — uses the shared dev MySQL DB
- NexOria backend and Admin UI can query this table by `hotel_id` or `job_id` to list all scraped assets
- Foundation for linking a completed scrape job to a specific hotel in the admin UI

---

## Files to Change

| File | Change |
|---|---|
| `main.py` | Remove SQLite `DATABASE_URL` override (lines 244–248) |
| `models/database.py` | Add `KBAsset` model + `Hotel.kb_assets` relationship + migration |
| `Kepsla-hotal-kb-scraper-main/models/job.py` | Add same `KBAsset` model on scraper Base |
| `Kepsla-hotal-kb-scraper-main/scraper/image_downloader.py` | Save image records to DB after download |
| `Kepsla-hotal-kb-scraper-main/generator/file_packager.py` | Save KB file record to DB after packaging |
