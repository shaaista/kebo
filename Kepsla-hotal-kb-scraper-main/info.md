# 1. Project Overview

**Project Name:** Hotel KB Scraper

Hotel KB Scraper is a standalone Python/FastAPI system that crawls hotel websites, detects individual properties across single-brand and multi-property hotel groups, and turns raw site content into structured knowledge-base artifacts for downstream chatbot/RAG ingestion. It combines automated discovery, human-in-the-loop review, GPT-4o-based structuring, image harvesting, and final bundle packaging in one operator-facing workflow.

**Problem it solves:** Hotel content is scattered across landing pages, room pages, restaurant pages, PDFs, images, offers pages, and multi-property sitemap structures. This project automates the extraction, cleanup, review, and packaging process so a team can generate ingestion-ready KB files without manually curating every hotel website.

# 2. Backend Architecture

- **Languages/Frameworks:** Python, FastAPI, Uvicorn, Jinja2, Pydantic Settings, SQLAlchemy Async, aiosqlite.
- **Architecture style:** Modular monolith with a REST API, a DB-backed internal job queue, and a separate long-running worker process.
- **Core runtime split:**
- `main.py` runs the FastAPI app, middleware, health, metrics, and UI shell.
- `worker.py` claims queued jobs from SQLite and executes long-running crawl/publish tasks out-of-band.
- `supervisor.py` can run API + worker as coordinated subprocesses.
- **Primary backend modules:**
- `api/routes/` exposes crawl, review, publish, job control, and download endpoints.
- `scraper/` handles URL discovery, crawling, anti-bot fallbacks, extraction, property grouping, and image download.
- `processor/` handles cleaning, section classification, review manifest generation, and GPT-4o KB structuring.
- `generator/` validates, formats, and packages artifacts into per-property folders and ZIP bundles.
- `services/` provides queueing, security, metrics, worker supervision, and job-state reconciliation.
- `models/job.py` persists job state, progress, queue metadata, and JSON payloads in SQLite.

# 3. API Layer

- **API type:** REST.
- **Key endpoints:**
- `POST /api/scrape` starts Phase 1 discovery/crawl/property-detection and enqueues the job.
- `GET /api/status/{job_id}` returns status, progress, queue state, and output metadata for polling.
- `GET /api/jobs` lists recent jobs/sessions for the operator UI.
- `GET /api/review/{job_id}` returns the review manifest used to enable/disable properties and content items.
- `POST /api/publish/{job_id}` starts Phase 2 for selected properties: expansion, KB generation, image download, packaging.
- `POST /api/jobs/{job_id}/stop` stops queued/running work and preserves resumable queue context.
- `POST /api/jobs/{job_id}/resume` requeues stopped work.
- `GET /api/download/{job_id}` downloads the generated ZIP bundle.
- `GET /api/download/{job_id}/{filename}` downloads a specific artifact with path validation.
- `GET /health` returns service and queue health.
- `GET /metrics` exposes Prometheus-style operational metrics.
- **Legacy/compatibility endpoints:** `/api/properties/{job_id}` and `/api/process/{job_id}` still support the older flow.
- **Async/sync handling:** FastAPI endpoints are async; HTTP crawling uses async clients and concurrency semaphores; OpenAI calls use `AsyncOpenAI`; SQLite uses async SQLAlchemy sessions; browser-heavy/synchronous fallbacks are isolated behind worker execution and executor-based helpers.
- **External integrations:** OpenAI GPT-4o, Scrapling, Crawl4AI, Playwright, curl_cffi, cloudscraper, httpx, trafilatura, pypdf, openpyxl.

# 4. Database & Storage

- **Database:** SQLite via `sqlite+aiosqlite`.
- **Persistence model:** A single `scrape_jobs` table stores job identity, status, progress counters, error messages, queue state, task payloads, review data, job context, preview text, output paths, timestamps, and worker heartbeat metadata.
- **Schema details:** JSON-heavy columns such as `properties_data`, `review_data`, and `job_context_data` let the app persist discovered properties, review selections, and publish context without requiring a separate document store.
- **DB optimizations:** WAL mode, `synchronous=NORMAL`, and `busy_timeout=30000` are configured to improve concurrent read/write behavior for the API and worker on a single-node SQLite deployment.
- **Storage model:** Generated artifacts are written to the local filesystem under the configured output directory, including per-property KB files, offer KB files, image folders, Excel image catalogs, combined text bundles, ZIP archives, and application logs (`output/logs/hotel-kb-scraper.log`).
- **Object storage:** None in-repo; artifact storage is local-disk based rather than S3/GCS/Azure Blob.

# 5. AI / ML Components

- **AI type:** LLM-based information extraction and document structuring; no model training or classical ML pipeline is present.
- **Model integration:** The processor layer uses OpenAI `gpt-4o` through `AsyncOpenAI` with deterministic settings (`temperature=0.0`) to convert messy hotel website content into schema-locked KB sections.
- **LLM pipeline:**
- Clean extracted page text and remove boilerplate/noise.
- Enrich pages with structured metadata, contacts, JSON-LD facts, image references, and selected high-signal PDF text.
- Classify pages into hotel-specific KB sections such as rooms, dining, amenities, spa, meetings, policies, location, contact info, and offers.
- Chunk large section inputs, run extraction prompts, and then merge partial outputs with a consolidation prompt.
- Run a dedicated offers extraction path so promotional packages are emitted as richer, separate offer KB artifacts when available.
- **RAG readiness:** Output is intentionally formatted with exact section headers (`=== SECTION ===`) for downstream chunking and ingestion into the separate chatbot system referenced in the repo notes.
- **Embeddings/vector search:** Not implemented in this repository.

# 6. Data Pipelines

- **Discovery/ingestion pipeline:**
- Fetch and interpret `robots.txt`.
- Parse sitemap indexes and sitemap XML files.
- Run bounded BFS link discovery for same-domain expansion.
- Score and filter URLs to suppress investor/corporate/noise pages while preserving hotel/property pages.
- Respect locale scope for sites that segment content by locale.
- Merge manually supplied specific URLs into the crawl set.
- **Crawl pipeline:** Multi-engine cascade of Scrapling Fetcher, Scrapling StealthyFetcher, Scrapling DynamicFetcher, anti-bot protection handler, Crawl4AI, and Playwright.
- **Extraction pipeline:** Trafilatura/readability/BeautifulSoup-based extraction of main text, markdown, tables, images, videos, files, contacts, structured data, and metadata.
- **Property pipeline:** Property-detection and property-expansion logic groups pages by hotel/property, synthesizes likely follow-up URLs, and drops obvious 404s or wrong-property pages.
- **Review pipeline:** `processor/review_manifest.py` builds a human-review manifest of properties and page/image/video/file items so an operator can rename, relabel, enable, or disable content before publication.
- **Image pipeline:** `scraper/image_downloader.py` downloads property images concurrently, skips low-value assets, and creates an Excel catalog per property.
- **Automation/scheduling:** Work is scheduled through the SQLite-backed queue; the worker polls for queued tasks, sends heartbeats, retries failures with host-specific backoff rules, and can be auto-managed by the API process or supervised explicitly.

# 7. Deployment & DevOps

- **Containerization:** No Dockerfile or Compose configuration is checked in; deployment is process-based rather than container-first.
- **Runtime model:** The app can run directly with `python main.py`, via `uvicorn main:app`, or under `supervisor.py`, which keeps the API server and worker subprocess alive together.
- **Server assumptions:** Designed for a single node with local disk, Playwright browser support, and filesystem access for generated bundles; this fits a VM or bare-metal deployment on Linux/Windows rather than a serverless target.
- **Configuration:** Environment-driven via `.env` / `.env.example`, including OpenAI key, port, crawler limits, retry policies, queue settings, rate limiting, and optional API Basic Auth.
- **Cloud/external services:** OpenAI is the primary external service dependency. There is no in-repo Terraform, Kubernetes, AWS, or CI/CD pipeline.
- **Operational hooks:** `/health` and `/metrics` support runtime checks and Prometheus-style scraping; `services/worker_supervisor.py` exposes managed-worker snapshots for diagnostics.

# 8. Performance & Scalability

- **Async processing:** Async endpoints, async HTTP fetching, async OpenAI calls, async SQLite access, and bounded semaphores are used throughout the hot path.
- **Concurrency controls:**
- Max pages per site: `3000`
- Max concurrent crawls: `10`
- Max concurrent KB generations: `4`
- Max concurrent property expansions: `4`
- Max concurrent property image downloads: `3`
- Max concurrent packaging tasks: `4`
- **Crawl optimization:** The crawler prefers Scrapling-first execution and only escalates to heavier browser automation when protection or JS rendering requires it.
- **Fault tolerance:** Queue retries are host-aware, stale worker sessions are interrupted automatically, stopped jobs are resumable, and completed filesystem artifacts can reconcile interrupted DB state back to `completed`.
- **Load profile:** This is optimized for operator-driven batch ingestion rather than high-QPS public API traffic; the design is realistic for tens of queued jobs on a single node with large, long-running background tasks.
- **Packaging efficiency:** Selected-property publishing prevents expensive extraction/LLM/image work on unwanted properties, which is important for hotel groups with large sitemaps.

# 9. Security & Reliability

- **Auth:** Optional HTTP Basic Auth protects `/api/*` and `/metrics`; credentials are env-driven.
- **Traffic controls:** In-memory sliding-window rate limiter is enabled by default and keyed off client IP / forwarded IP.
- **Path safety:** Download routes explicitly reject traversal attempts and validate resolved paths against the job output directory.
- **Error handling:** Retryable HTTP failures are wrapped in a dedicated retry policy system with host-specific attempts, backoff schedules, and retryable status-code lists.
- **Observability:** Request IDs are injected per request; API latency, auth failures, queue depth, worker outcomes, and rate-limit rejections are exported via a Prometheus-compatible metrics endpoint.
- **Reliability tests:** The test suite covers retry policies, anti-bot interstitial detection, queue stop/resume behavior, stale-worker interruption, review availability, artifact recovery, publish selection, packaging concurrency, and browser-visible progress state.
- **Security note:** CORS is open to all origins in the current app, which is fine for an internal operator tool but would need tightening for a public internet-facing deployment.

# 10. Tech Stack

- **Backend:** Python, FastAPI, Uvicorn, Jinja2, Pydantic, Pydantic Settings, SQLAlchemy Async, aiosqlite
- **Database:** SQLite, WAL mode, JSON-backed job context storage
- **DevOps / Ops:** `supervisor.py`, environment-based config, Prometheus-style `/metrics`, request logging, Playwright browser runtime
- **AI:** OpenAI GPT-4o, prompt-based section extraction/merge, PDF text enrichment
- **Tools / Data Extraction:** Scrapling, Crawl4AI, Playwright, curl_cffi, cloudscraper, httpx, BeautifulSoup4, lxml, trafilatura, markdownify, readability-lxml, pypdf, openpyxl

# 11. Impact & Scale

- The system is configured to crawl up to **3,000 pages per site** with **10 concurrent crawl tasks**, which is materially larger than a simple brochure-site scraper and appropriate for hotel groups with location and property hierarchies.
- The publish path can fan out into **4 parallel KB generations**, **4 property expansions**, **3 concurrent image-download jobs**, and **4 packaging tasks**, making it practical for batch processing multi-property hotel portfolios on one node.
- Repo progress notes document the project being exercised against a Sarovar Hotels-style sitemap with roughly **149-150 detected properties**, **10,319 downloaded images**, and about **3.1 GB of media artifacts**, showing that the design targets chain-scale rather than single-property-only workloads.
- The same progress notes record a Scrapling-first benchmark of **15 pages in ~11.2 seconds** versus **~228.4 seconds** for a heavier browser-driven fallback path, indicating an order-of-magnitude improvement when fast-path crawling succeeds.
- Realistic production usage is a **single-node batch-ingestion service** handling tens of queued jobs, hundreds to thousands of pages per batch, and multi-GB output bundles rather than a consumer-facing, multi-tenant SaaS with internet-scale request volume.

# 12. Resume Bullet Points

- Designed and built a modular **FastAPI + SQLAlchemy** backend that converts hotel websites into ingestion-ready knowledge bases through a two-phase pipeline: discovery/crawl/property detection, followed by operator-reviewed publishing and artifact packaging.
- Engineered a **SQLite-backed async job queue and worker system** with heartbeats, retry scheduling, stop/resume controls, stale-worker recovery, and managed subprocess supervision, eliminating the need for Redis/Celery in a single-node production workflow.
- Implemented a resilient **multi-engine web ingestion stack** using Scrapling, Crawl4AI, Playwright, curl_cffi, cloudscraper, and httpx to handle static pages, JS-heavy flows, and anti-bot interstitials across complex hotel group domains.
- Built property-detection and expansion logic that groups multi-property hotel sites into per-property datasets and has been exercised against networks with **~150 hotels, 10k+ images, and multi-GB artifact output**.
- Optimized crawl throughput with **async concurrency controls, WAL-tuned SQLite, and parallel publishing/image-packaging pipelines**; project benchmarks show a fast-path crawl improvement from **~228s to ~11s** on representative pages.
- Developed a **GPT-4o-based KB structuring pipeline** that cleans noisy website copy, enriches inputs with JSON-LD/PDF/menu data, classifies content into hotel-specific sections, and emits schema-locked KB text plus dedicated offers knowledge bases.
- Delivered end-to-end artifact generation including **per-property KB files, combined bundle files, ZIP downloads, image folders, and Excel image catalogs**, enabling direct handoff into downstream chatbot/RAG ingestion workflows.
- Hardened the service with **optional Basic Auth, rate limiting, Prometheus-style metrics, secure download path validation, and structured request logging**, improving operational reliability for long-running crawl jobs.
