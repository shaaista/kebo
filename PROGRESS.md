# Kebo / NexOria — Progress Tracker

---

## 2026-04-21/22 — Production Docker Deployment

**Status:** Completed

**What was done:**
- Created `admin_ui/Dockerfile` — builds React app with `npm run build` before serving via Nginx. Fixes white screen and MIME type errors.
- Created `admin_ui/nginx.conf` — Nginx routes traffic: `/admin/` to React, `/api/` and `/admin/api/` to FastAPI, `/chat` to built chat HTML, `/kb-scraper/` redirects to `/admin/app/crawl`, `/static/` to FastAPI, scraper JS/CSS to port 8501.
- Updated root `Dockerfile` — changed CMD from `uvicorn main:app` to `python main.py` so KB scraper subprocess also starts. Added EXPOSE 8501.
- Updated `docker-compose.yml` — added `expose: 8501` so Nginx can reach scraper internally.
- Removed SQLite override in `main.py` (lines 247-251) — scraper now uses MySQL from `.env` instead of isolated SQLite file.
- Added Docker/Nginx files to `.gitignore` — these are managed manually on the server via MobaXterm, not through GitHub.

**Files changed:**
- `admin_ui/Dockerfile` (new — manual on server)
- `admin_ui/nginx.conf` (new — manual on server)
- `Dockerfile` (manual on server)
- `docker-compose.yml` (manual on server)
- `main.py` — removed SQLite override
- `.gitignore` — added Docker/Nginx entries

**Deploy process:**
1. Push code changes to GitHub
2. SSH into server via MobaXterm
3. `git pull`
4. Manually update Docker/Nginx files via `nano` if changed
5. `docker-compose down -v`
6. `docker-compose up -d --build`

**What works now:**
- `/admin/` — admin panel ✅
- `/admin/app/crawl` — crawler page ✅
- `/kb-scraper/` — redirects to crawl page ✅
- `/chat` — chat widget ✅
- `/health` — health check ✅
- Chat API (LLM) ✅
- KB scraper running and connected to MySQL ✅
- Frontend stays alive if backend crashes ✅

---

## Next — MySQL Without VPN

**Status:** Research needed

**Problem:** MySQL database at `172.16.5.32:3306` is only reachable via VPN. If the AWS server loses VPN connection, the entire product stops working.

**See:** `docs/plans/mysql-without-vpn.md` (to be created)
