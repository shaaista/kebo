# Hotel KB Scraper - Progress

## Project Overview
**Purpose**: Standalone web tool for scraping hotel websites and generating KB files for kebo-main chatbot.
**Location**: `C:/Users/venky/Desktop/kepsla.ai/hotel-kb-scraper/` (separate from kebo-main)
**Stack**: FastAPI + Jinja2 + Tailwind CSS | Scrapling (primary) + Crawl4AI + Playwright + curl_cffi (fallbacks) | OpenAI GPT-4o | SQLite
**Port**: 8501 (kebo-main runs on 8000)

## How to Run
```bash
cd C:/Users/venky/Desktop/kepsla.ai/hotel-kb-scraper
pip install -r requirements.txt
playwright install chromium
cp .env.example .env  # Add your OPENAI_API_KEY
python main.py        # Opens on http://localhost:8501
```

---

## 2026-03-23 - Project Initialization
**Status**: Completed
**What was done**: Created project structure, config files, data models
**Files**: config/settings.py, config/hotel_kb_schema.py, models/job.py, requirements.txt, .env.example

## 2026-03-23 - Scraper Module (6 Files)
**Status**: Completed
**What was done**: Full crawl pipeline — URL filtering, sitemap/BFS discovery, Cloudflare bypass (curl_cffi + cloudscraper), JS rendering (Crawl4AI + Playwright), content extraction (trafilatura + BS4), multi-property detection
**Files**: scraper/url_filter.py, discovery.py, protection_handler.py, crawl_engine.py, content_extractor.py, orchestrator.py

## 2026-03-23 - Processor Module (3 Files)
**Status**: Completed
**What was done**: Text cleaning (boilerplate removal, dedup), section classification (keyword scoring), LLM structuring (OpenAI chunked extraction + merge)
**Files**: processor/text_cleaner.py, section_classifier.py, llm_structurer.py

## 2026-03-23 - Generator Module (3 Files)
**Status**: Completed
**What was done**: Multi-format output (.txt for kebo-main, .json structured, .md readable), KB validation (completeness + quality scoring), ZIP packaging for multi-property
**Files**: generator/kb_formatter.py, kb_validator.py, file_packager.py

## 2026-03-23 - API + Main Entry Point (3 Files)
**Status**: Completed
**What was done**: POST /api/scrape (start job), GET /api/status (poll), GET /api/jobs (list), GET /api/download (ZIP/individual files), main.py with lifespan, CORS, static mount
**Files**: api/routes/scrape.py, api/routes/download.py, main.py

## 2026-03-23 - Frontend UI (3 Files)
**Status**: Completed
**What was done**: Dark theme UI with Tailwind — URL input, 5-step progress tracker, KB preview, download buttons, recent jobs table. Vanilla JS polling.
**Files**: templates/index.html, static/css/style.css, static/js/app.js

## 2026-03-23 - Scrapling Primary Crawl Engine Rewrite
**Status**: Completed
**What was done**: Rewrote crawl_engine.py to make Scrapling the PRIMARY crawling tool with all existing tools as fallbacks. New 6-step cascade: Scrapling Fetcher -> StealthyFetcher -> DynamicFetcher -> protection_handler -> Crawl4AI -> Playwright. Added 3 new async functions (_crawl_scrapling_fast, _crawl_scrapling_stealth, _crawl_scrapling_dynamic) with run_in_executor wrapping. Added detailed per-step logging (>>>, <<<, xxx prefixes) with timing. Graceful degradation via _SCRAPLING_AVAILABLE flag. All existing functions preserved. Added _extract_text_cascade helper and _crawl_with_protection_handler wrapper. Enhanced batch logging with progress percentages and success/fail counts.
**Files changed**: scraper/crawl_engine.py

## 2026-03-24 - Scrapling Integration (All 3 Scraper Files Rewritten)
**Status**: Completed
**What was done**: Made Scrapling the PRIMARY tool across all scraper modules, with all existing tools as FALLBACKS. Added comprehensive logging everywhere.

### protection_handler.py
- New 5-method cascade: scrapling_fast -> scrapling_stealth -> httpx -> curl_cffi -> cloudscraper
- Added `_fetch_scrapling_fast()` (Fetcher with TLS impersonation)
- Added `_fetch_scrapling_stealth()` (StealthyFetcher with CF bypass)
- Detailed logging: `>>> [METHOD] Attempting`, `<<< [METHOD] SUCCESS`, `xxx [METHOD] FAILED`
- Timing via `time.perf_counter()` for every method
- `=== FETCH SUMMARY` log line with method, status, attempts count, elapsed time
- Graceful degradation: `_SCRAPLING_AVAILABLE` flag, SKIPPED logs when not installed

### crawl_engine.py
- New 6-step cascade: Scrapling Fetcher -> StealthyFetcher -> DynamicFetcher -> protection_handler -> Crawl4AI -> Playwright
- 3 new async functions: `_crawl_scrapling_fast`, `_crawl_scrapling_stealth`, `_crawl_scrapling_dynamic`
- All wrapped in `run_in_executor` (Scrapling is synchronous)
- New `_extract_text_cascade()` helper (trafilatura -> readability -> BS4)
- New `_crawl_with_protection_handler()` wrapper
- `=== CRAWL START/RESULT` and `=== BATCH START/PROGRESS/COMPLETE` logging
- All existing functions preserved as fallback path

### discovery.py
- New `_fetch_page()` helper: tries Scrapling first, httpx fallback
- New `_extract_links_scrapling()` using Scrapling Adaptor CSS selectors
- `_extract_links_bs4()` as fallback for link extraction
- Phase-level timing: robots.txt, sitemaps, BFS each timed separately
- `=== DISCOVERY COMPLETE` summary with sitemap/bfs breakdown and total time
- BFS progress logging every 10 pages

### requirements.txt
- Added `scrapling>=0.2.0` as PRIMARY dependency (top of file)
- All existing deps remain as FALLBACK dependencies with clear comments

**Files changed**: scraper/protection_handler.py, scraper/crawl_engine.py, scraper/discovery.py, requirements.txt

## 2026-03-24 - Scrapling v0.4.2 API Fix + Test Run
**Status**: Completed
**What was done**: Fixed Scrapling API changes in v0.4.2 — `resp.html` → `str(resp.html_content)`, `Fetcher(auto_match=False)` → `Fetcher()`, `Adaptor` → `Selector`. Ran test against sarovarhotels.com. Results: 15/15 pages crawled via scrapling_fast in 11.2s (was 228.4s via Playwright), 122K chars extracted (was 30K). 20x faster crawling.
**Files changed**: scraper/crawl_engine.py, scraper/protection_handler.py, scraper/discovery.py

## 2026-03-24 - Multi-Property Detection + Settings Upgrade
**Status**: Completed
**What was done**: Rewrote `detect_properties()` in orchestrator.py with URL-slug-based grouping for hotel group sites. First path segment = property slug (e.g., `/hometel-chandigarh/rooms.html` → "Hometel Chandigarh"). Non-property slugs (blogs, offers, about-us, etc.) go to "General" bucket. Tested against sarovarhotels.com full sitemap (2,596 URLs) — correctly detected 149 individual hotel properties with 9-64 pages each. Increased max_pages_per_site from 50→3000, max_concurrent_crawls from 5→10, crawl_delay from 1.5→0.5s.
**Files changed**: scraper/orchestrator.py, config/settings.py, .env

## 2026-03-24 - Image Downloader + Excel Catalog
**Status**: Completed
**What was done**: Created `scraper/image_downloader.py` — downloads all property images concurrently and creates a styled Excel catalog per property.
- `download_property_images()` — collects unique images from extracted content, downloads concurrently (semaphore-limited)
- `_create_excel_catalog()` — creates Excel with columns: #, Title, Image URL, Description, Source Page, Filename, File Size (KB). Styled headers + summary sheet.
- `_should_skip_image()` — filters out icons, favicons, trackers, spacers, SVGs, tiny (<5KB) images
- `_make_safe_filename()` — creates descriptive filenames like `001_deluxe-room-interior.jpg`
- Added `openpyxl>=3.1.0` to requirements.txt
- Integrated into orchestrator.py as Step 5 (after extraction, before returning results)
- Output structure: `output/<job_id>/<property>/images/` + `<property>_images.xlsx`
**Files**: scraper/image_downloader.py (NEW), requirements.txt, scraper/orchestrator.py

## 2026-03-24 - Full Pipeline Test Rewrite
**Status**: Completed
**What was done**: Rewrote `test_sarovar.py` to use `run_scrape_job()` from orchestrator instead of running phases separately. Now tests the FULL pipeline: Discovery → Crawl → Property Detection → Extraction → Image Download. Fixed `MAX_DEPTH=3→4` in .env so BFS can reach individual property subpages (homepage→city→property→subpage).
**Files changed**: test_sarovar.py, .env

### Why Only 1 Hotel Was Extracted Before
The old test script ran discovery/crawl/extraction as separate steps on a 15-page sample. It **never called** `detect_properties()` or `download_property_images()`. The 15 pages were all city listing pages from the sitemap (hotels-in-agra.html, etc.), not individual property pages. The new test uses the orchestrator which runs ALL stages including multi-property detection.

### Pipeline Architecture (Complete)
```
1. DISCOVERY    → Sitemap + BFS crawl → find all URLs (up to 3000)
2. CRAWLING     → Scrapling 6-step cascade → fetch HTML for each page
3. DETECTION    → URL slug grouping → group pages by hotel property
4. EXTRACTION   → trafilatura + BS4 + markdownify → structured content per page
5. IMAGES       → Download images + create Excel catalog per property
6. LLM (API)    → GPT-4o structuring → section-based KB text per property
7. PACKAGING    → .txt/.json/.md + validation + ZIP bundle
```
Steps 1-5 run via `run_scrape_job()` (orchestrator).
Steps 6-7 run via `_run_full_pipeline()` (API route, requires OpenAI key).

### Output Directory Structure
```
output/<job_id>/
├── <property_1>/
│   ├── images/
│   │   ├── 001_deluxe-room.jpg
│   │   ├── 002_lobby-view.jpg
│   │   └── ...
│   ├── <property_1>_images.xlsx      ← Excel catalog
│   ├── <property_1>_kb.txt           ← kebo-main compatible
│   ├── <property_1>_kb.json          ← structured JSON
│   ├── <property_1>_kb.md            ← readable markdown
│   └── validation_report.json
├── <property_2>/
│   └── ... (same structure)
└── <job_id>_all_kbs.zip              ← ZIP of everything
```

### Key Settings (.env)
| Setting | Value | Why |
|---------|-------|-----|
| MAX_PAGES_PER_SITE | 3000 | Sarovar has 2500+ pages across 150+ properties |
| MAX_DEPTH | 4 | homepage→city→property→subpage (4 levels deep) |
| MAX_CONCURRENT_CRAWLS | 10 | Balance speed vs politeness |
| CRAWL_DELAY_SECONDS | 0.5 | Polite delay between requests |

## 2026-03-24 - Two-Phase Pipeline + Property Selection UI
**Status**: Completed
**What was done**: Split the pipeline into 2 phases with a PAUSE for property selection, so users pick which hotels to process (avoids rate limits and unnecessary work).

### Flow
```
Phase 1 (automatic):
  POST /api/scrape → Discovery → Crawl → Property Detection → PAUSE
  Status: "properties_detected" — UI shows property list with toggles

Phase 2 (user-triggered):
  POST /api/process/{job_id} → Extract → Images → LLM KB → Package → ZIP
  Only processes user-selected properties
```

### Backend Changes
- **models/job.py**: Added `PROPERTIES_DETECTED` and `DOWNLOADING_IMAGES` statuses, added `properties_data` column (JSON)
- **scraper/orchestrator.py**: Split into `run_discovery_phase()` and `run_processing_phase()`. Legacy `run_scrape_job()` preserved for test scripts.
- **api/routes/scrape.py**: Added 3 new endpoints:
  - `GET /api/properties/{job_id}` — returns property list for selection UI
  - `POST /api/process/{job_id}` — starts Phase 2 with selected properties
  - In-memory `_crawl_data_store` holds crawl data between phases
- Old DB deleted (schema changed with new `properties_data` column)

### Frontend Changes
- **templates/index.html**: New "Property Selection" section with:
  - Checkbox toggles per property (name, page count, sample URLs)
  - Select All / Deselect All buttons
  - Search filter to find specific properties
  - "Process Selected" button to start Phase 2
  - 7 pipeline steps shown (was 5): Discovery → Crawling → Property Detection → Extracting → Images → KB Gen → Complete
- **static/js/app.js**: Rewritten for 2-phase flow:
  - `startScrape()` → Phase 1 only
  - `loadProperties()` → fetches detected properties on pause
  - `renderPropertyList()` → renders toggles with search/filter
  - `processSelected()` → starts Phase 2 with checked properties
  - Button text: "Discover Properties" (was "Generate KB")
- **static/css/style.css**: Added paused state (amber progress bar), property list scrollbar, checkbox styling, new status pill colors

### Test Results (Previous Run)
- 150 properties detected from sarovarhotels.com
- 10,319 images downloaded (3.1 GB)
- 150 Excel catalogs created
- Scrapling Fast handled 100% of crawling

**Files changed**: models/job.py, scraper/orchestrator.py, api/routes/scrape.py, templates/index.html, static/js/app.js, static/css/style.css

## Next Steps
- [ ] Test the full 2-phase UI flow on localhost:8501
- [ ] Run LLM KB generation for selected properties
- [ ] Test KB output quality and kebo-main compatibility
- [ ] Add .gitignore
