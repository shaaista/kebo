# Graph Report - .  (2026-04-21)

## Corpus Check
- 37 files · ~62,757 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 713 nodes · 1286 edges · 32 communities detected
- Extraction: 80% EXTRACTED · 20% INFERRED · 0% AMBIGUOUS · INFERRED: 258 edges (avg confidence: 0.5)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 22|Community 22]]
- [[_COMMUNITY_Community 23|Community 23]]
- [[_COMMUNITY_Community 24|Community 24]]
- [[_COMMUNITY_Community 25|Community 25]]
- [[_COMMUNITY_Community 26|Community 26]]
- [[_COMMUNITY_Community 27|Community 27]]
- [[_COMMUNITY_Community 28|Community 28]]
- [[_COMMUNITY_Community 29|Community 29]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 31|Community 31]]

## God Nodes (most connected - your core abstractions)
1. `ScrapeJob` - 92 edges
2. `JobStatus` - 91 edges
3. `SiteBlockedError` - 45 edges
4. `RetryableHttpError` - 38 edges
5. `discover_urls()` - 15 edges
6. `_derive_property_display_name()` - 14 edges
7. `_extract_text_cascade()` - 13 edges
8. `structure_property_kb()` - 12 edges
9. `QueuedTask` - 12 edges
10. `MetricsRegistry` - 12 edges

## Surprising Connections (you probably didn't know these)
- `Discover all crawlable pages from a hotel website URL.  Uses robots.txt, sitemap` --uses--> `RetryableHttpError`  [INFERRED]
  scraper\discovery.py → scraper\retry_policy.py
- `Return True when the page uses a JS framework that needs Playwright     for reli` --uses--> `RetryableHttpError`  [INFERRED]
  scraper\discovery.py → scraper\retry_policy.py
- `Re-fetch *url* with Playwright (networkidle) and return all links.      This is` --uses--> `RetryableHttpError`  [INFERRED]
  scraper\discovery.py → scraper\retry_policy.py
- `Cap queued links per page so one noisy hub page cannot dominate discovery.` --uses--> `RetryableHttpError`  [INFERRED]
  scraper\discovery.py → scraper\retry_policy.py
- `Allow discovery to over-collect before final ranking trims the result set.` --uses--> `RetryableHttpError`  [INFERRED]
  scraper\discovery.py → scraper\retry_policy.py

## Communities

### Community 0 - "Community 0"
Cohesion: 0.03
Nodes (157): Base, BaseModel, download_job_zip(), download_specific_file(), API endpoints for downloading generated KB files., Pick the ZIP for the current bundle, otherwise fall back to the newest archive., Download a ZIP archive containing all KB files for a completed job.      Returns, Download a specific KB file from a job's output directory.      The file is sear (+149 more)

### Community 1 - "Community 1"
Cohesion: 0.06
Nodes (82): Raised when a site is permanently blocked by bot-protection.      Caught by the, SiteBlockedError, Exception, _build_property_aliases(), _categorize_property_url(), _clean_property_candidate(), _compact_text(), _dedupe_strings() (+74 more)

### Community 2 - "Community 2"
Cohesion: 0.06
Nodes (53): _asset_search_text(), _build_offer_kb_text(), build_sections_list(), _call_llm(), chunk_text(), _collect_offer_pages(), _compact_structured_text(), _dedupe_pages_by_source() (+45 more)

### Community 3 - "Community 3"
Cohesion: 0.08
Nodes (49): _build_dynamic_thresholds(), _classify_page_type(), crawl_page(), crawl_pages_batch(), _crawl_scrapling_dynamic(), _crawl_scrapling_fast(), _crawl_scrapling_stealth(), _crawl_with_bright_data() (+41 more)

### Community 4 - "Community 4"
Cohesion: 0.06
Nodes (48): _bfs_crawl(), discover_urls(), _discovery_collection_limit(), _extract_external_property_urls(), _extract_links_bs4(), _extract_links_scrapling(), _extract_locale_prefix(), _fetch_and_extract_links_dynamic() (+40 more)

### Community 5 - "Community 5"
Cohesion: 0.12
Nodes (25): _build_asset_item(), _build_page_item(), build_review_data(), build_review_entity(), _display_path(), _filename_from_url(), _match_mapping(), Build review-ready content manifests for the UI content manager. (+17 more)

### Community 6 - "Community 6"
Cohesion: 0.1
Nodes (25): clean_text(), _decode_html_entities(), deduplicate_content(), merge_texts(), _normalize_whitespace(), Clean and prepare raw extracted text for LLM processing.  Takes messy HTML-extra, Remove zero-width characters, BOM markers, and other invisible unicode., Decode any remaining HTML entities that weren't handled during extraction. (+17 more)

### Community 7 - "Community 7"
Cohesion: 0.13
Nodes (23): _clean_text(), extract_contact_info(), extract_content(), _extract_images(), _extract_linked_assets(), _extract_main_text(), _extract_meta(), extract_structured_data() (+15 more)

### Community 8 - "Community 8"
Cohesion: 0.11
Nodes (23): detect_protection_interstitial(), _fetch_cloudscraper(), _fetch_curl_cffi(), _fetch_httpx(), _fetch_scrapling_fast(), _fetch_scrapling_stealth(), fetch_with_protection(), _fetch_with_protection_once() (+15 more)

### Community 9 - "Community 9"
Cohesion: 0.11
Nodes (12): MetricsRegistry, In-memory metrics registry with Prometheus exposition output., Render Prometheus text exposition output., Track bounded-cardinality counters and emit simple alerts., Reset all in-memory counters., Record API request traffic and latency., Record an authentication failure and evaluate alerts., Record a rate-limit rejection and evaluate alerts. (+4 more)

### Community 10 - "Community 10"
Cohesion: 0.15
Nodes (21): _contains_discovery_noise(), deduplicate_urls(), extract_domain(), is_same_domain(), normalize_url(), _path_segments(), _path_tokens(), prioritize_discovery_links() (+13 more)

### Community 11 - "Community 11"
Cohesion: 0.14
Nodes (19): _create_excel_catalog(), _download_one_image(), download_property_images(), _get_extension(), _looks_generic_stem(), _looks_like_sentence(), _make_safe_filename(), Download property images and create an Excel catalog.  For each hotel property: (+11 more)

### Community 12 - "Community 12"
Cohesion: 0.14
Nodes (17): _build_combined_kb_text(), build_job_bundle_name(), _create_bundle_zip(), _create_zip(), _merge_existing_property_artifacts(), package_all_properties(), package_kb(), Package KB files into downloadable text bundles and save to the output directory (+9 more)

### Community 13 - "Community 13"
Cohesion: 0.14
Nodes (14): favicon(), health_check(), index(), lifespan(), metrics_endpoint(), protect_and_log_requests(), _protected_path(), FastAPI application entry point for Hotel KB Scraper. (+6 more)

### Community 14 - "Community 14"
Cohesion: 0.13
Nodes (0): 

### Community 15 - "Community 15"
Cohesion: 0.2
Nodes (13): _build_page_signals(), _canonical_page_key(), classify_page(), group_content_by_section(), Classify extracted page content into hotel KB sections., Count how many keyword matches a text has for a given section., Collect normalized page signals used for section scoring., Return qualifying section scores for a page. (+5 more)

### Community 16 - "Community 16"
Cohesion: 0.15
Nodes (9): get_client_identifier(), has_valid_basic_auth(), InMemoryRateLimiter, Authentication and rate-limiting helpers., Simple per-client sliding-window rate limiter., Return ``True`` when the request should be allowed., Clear all recorded request history., Validate an incoming Basic Auth header. (+1 more)

### Community 17 - "Community 17"
Cohesion: 0.21
Nodes (11): extract_facts(), format_json(), format_md(), format_txt(), parse_sections(), Convert structured KB text into multiple output formats (.txt, .json, .md).  Tak, Extract individual facts from a section's content.      A "fact" is any informat, Format KB text for direct upload to kebo-main's knowledge base folder.      Adds (+3 more)

### Community 18 - "Community 18"
Cohesion: 0.24
Nodes (9): _calculate_quality_score(), _check_repetition(), generate_validation_summary(), Validate generated KB documents for completeness and quality.  Checks the struct, Compute an overall quality score from 0.0 to 1.0.      Weighted components:, Check for excessive phrase repetition in the KB text.      Looks for phrases (3+, Generate a concise human-readable summary of the validation report.      Args:, Validate a KB text document and produce a detailed report.      Checks section p (+1 more)

### Community 19 - "Community 19"
Cohesion: 0.2
Nodes (9): _configure_sqlite_connection(), get_session(), init_db(), _migrate_sqlite_schema(), Database models for scrape job tracking., Reduce writer contention for SQLite-backed local runs., Apply lightweight schema upgrades for local SQLite databases only.      On MySQL, Create tables if they don't exist.      - SQLite (local dev): also runs lightwei (+1 more)

### Community 20 - "Community 20"
Cohesion: 0.22
Nodes (8): BaseSettings, Application settings loaded from environment variables., Resolve relative filesystem paths against the repository root., Anchor local SQLite files to the repository root instead of process cwd., Central configuration for Hotel KB Scraper., _resolve_project_path(), _resolve_sqlite_database_url(), Settings

### Community 21 - "Community 21"
Cohesion: 0.25
Nodes (8): _build_worker_command(), get_managed_worker_snapshot(), maybe_start_managed_worker(), Manage an optional worker subprocess owned by the API process., Start a worker subprocess if managed auto-start is enabled and no worker is runn, Terminate the worker subprocess started by this API process., Return a lightweight status snapshot for the managed worker., shutdown_managed_worker()

### Community 22 - "Community 22"
Cohesion: 0.46
Nodes (7): _configure_worker_logging(), execute_job_task(), _heartbeat_loop(), _load_job(), main(), process_next_queued_job(), worker_loop()

### Community 23 - "Community 23"
Cohesion: 0.67
Nodes (3): main(), Simple process supervisor for the API server and worker., _spawn_process()

### Community 24 - "Community 24"
Cohesion: 0.67
Nodes (1): Service helpers for background jobs, security, and observability.

### Community 25 - "Community 25"
Cohesion: 1.0
Nodes (1): Hotel KB output schema — defines the sections and structure that the final KB fi

### Community 26 - "Community 26"
Cohesion: 1.0
Nodes (0): 

### Community 27 - "Community 27"
Cohesion: 1.0
Nodes (0): 

### Community 28 - "Community 28"
Cohesion: 1.0
Nodes (0): 

### Community 29 - "Community 29"
Cohesion: 1.0
Nodes (0): 

### Community 30 - "Community 30"
Cohesion: 1.0
Nodes (0): 

### Community 31 - "Community 31"
Cohesion: 1.0
Nodes (0): 

## Knowledge Gaps
- **183 isolated node(s):** `FastAPI application entry point for Hotel KB Scraper.`, `Handle startup and shutdown events for the application.`, `Apply auth, rate limiting, request logging, and metrics.`, `Render the main UI page.`, `Silence missing favicon requests in local development.` (+178 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **Thin community `Community 25`** (2 nodes): `hotel_kb_schema.py`, `Hotel KB output schema — defines the sections and structure that the final KB fi`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 26`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 27`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 28`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 29`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 30`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.
- **Thin community `Community 31`** (1 nodes): `__init__.py`
  Too small to be a meaningful cluster - may be noise or needs more connections extracted.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `SiteBlockedError` connect `Community 1` to `Community 4`?**
  _High betweenness centrality (0.031) - this node is a cross-community bridge._
- **Why does `RetryableHttpError` connect `Community 1` to `Community 8`, `Community 4`?**
  _High betweenness centrality (0.026) - this node is a cross-community bridge._
- **Why does `ScrapeJob` connect `Community 0` to `Community 19`?**
  _High betweenness centrality (0.026) - this node is a cross-community bridge._
- **Are the 89 inferred relationships involving `ScrapeJob` (e.g. with `Background worker process for queued scrape jobs.` and `Ensure the standalone worker writes to the shared application log.`) actually correct?**
  _`ScrapeJob` has 89 INFERRED edges - model-reasoned connections that need verification._
- **Are the 89 inferred relationships involving `JobStatus` (e.g. with `Background worker process for queued scrape jobs.` and `Ensure the standalone worker writes to the shared application log.`) actually correct?**
  _`JobStatus` has 89 INFERRED edges - model-reasoned connections that need verification._
- **Are the 40 inferred relationships involving `SiteBlockedError` (e.g. with `RetryableHttpError` and `Coordinates the entire scrape pipeline from URL discovery to content extraction.`) actually correct?**
  _`SiteBlockedError` has 40 INFERRED edges - model-reasoned connections that need verification._
- **Are the 34 inferred relationships involving `RetryableHttpError` (e.g. with `SiteBlockedError` and `Discover all crawlable pages from a hotel website URL.  Uses robots.txt, sitemap`) actually correct?**
  _`RetryableHttpError` has 34 INFERRED edges - model-reasoned connections that need verification._