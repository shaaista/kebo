# Production Readiness Plan

## Current State (Development)

When you run `python main.py`, three things start together as child processes:

```
python main.py
  ├── Vite dev server   (port 8080)  — React admin UI in dev/hot-reload mode
  ├── KB Scraper        (port 8501)  — hotel website crawler (FastAPI)
  └── FastAPI backend   (port 8011)  — main chatbot API
```

This works on a developer laptop but is **not safe or stable for production**.

---

## The Routing Problem

Right now there are two layers of proxying:

- `admin_ui/vite.config.ts` — Vite proxies `/admin/api`, `/api`, `/kb-scraper/api` to FastAPI
- `main.py` (line ~860) — FastAPI reverse-proxies `/kb-scraper/*` to scraper on port 8501

In production Vite won't run, so all the Vite proxy rules break. That is the routing problem the developer is referring to.

---

## Gap Checklist

| # | Gap | Current State | What Production Needs | Priority |
|---|---|---|---|---|
| 1 | **Frontend build** | Vite dev server running live | `npm run build` once → serve static `dist/` folder | 🔴 Critical |
| 2 | **Reverse proxy (Nginx)** | Vite + FastAPI both proxying | Nginx as single traffic cop in front of everything | 🔴 Critical |
| 3 | **Service isolation** | All 3 services launched by one Python script | Each service runs independently via systemd / Docker | 🔴 Critical |
| 4 | **Database without VPN** | MySQL at `172.16.5.32` requires VPN; scraper forced to SQLite | DB reachable from deployment server without VPN | 🔴 Critical |
| 5 | **HTTPS / SSL** | Plain `http://` only | SSL certificate on every public endpoint | 🔴 Critical |
| 6 | **Secrets management** | MySQL password + OpenAI key in plain `.env` file | Server environment variables, never in files/git | 🟠 High |
| 7 | **CORS** | Likely open (`*`) | Locked to actual production domain | 🟠 High |
| 8 | **Logging to files** | Console output only — lost on terminal close | Rotating log files on disk | 🟡 Medium |
| 9 | **Auto-restart / health checks** | No restart on crash | systemd/Docker watches `/health` endpoint and restarts | 🟡 Medium |

---

## What Each Gap Means (Plain English)

### 1. Build the Frontend Once
- **Now:** Vite runs as a live server, recompiles code on every change, uses lots of memory.
- **Fix:** Run `cd admin_ui && npm run build`. Creates `admin_ui/dist/` with plain HTML/JS/CSS.
  FastAPI already serves this folder — just needs `APP_ENV=production` so Vite doesn't start.

### 2. Put Nginx in Front
- **Now:** No dedicated web server. FastAPI serves static files AND proxies to the scraper.
- **Fix:** Nginx sits in front and routes:
  - `/admin/*` → serves `admin_ui/dist/` static files directly
  - `/api/*` → forwards to FastAPI on port 8011
  - `/kb-scraper/*` → forwards to KB scraper on port 8501
  - All other traffic → FastAPI on port 8011

### 3. Run Each Service Independently
- **Now:** All 3 services are child processes of `main.py`. One crash kills everything.
  We already hit this — orphaned scraper processes blocked port 8501.
- **Fix:** systemd service files or Docker containers for each of:
  - FastAPI backend (`main.py` minus subprocess management)
  - KB scraper (`Kepsla-hotal-kb-scraper-main/main.py` directly)
  - Nginx

### 4. Database Reachability
- **Now:** MySQL at `172.16.5.32` requires VPN. Scraper overrides to SQLite to avoid the VPN requirement.
  See `main.py` lines 244–248.
- **Fix:** Deploy MySQL (or migrate to a cloud DB like RDS/PlanetScale) on a server reachable
  without VPN. Remove the SQLite override. Both apps use the same MySQL DB.

### 5. HTTPS
- **Now:** Everything is `http://`. Browsers block API calls from HTTPS pages to HTTP servers.
- **Fix:** SSL certificate (free via Let's Encrypt / Certbot) on the Nginx server.
  All traffic becomes `https://`.

### 6. Secrets Out of Code
- **Now:** `.env` contains `zapcom123`, OpenAI key `sk-proj-...`, etc. in plain text.
- **Fix:** On the server, set these as OS environment variables or use a secrets manager.
  `.env` should only exist locally and be in `.gitignore`.

### 7. CORS Lockdown
- **Now:** CORS middleware in FastAPI — check if `allow_origins=["*"]` is set.
- **Fix:** Change to the actual production domain e.g. `allow_origins=["https://your-hotel-domain.com"]`.

### 8. Log Files
- **Now:** Logs print to terminal. Closed terminal = lost logs.
- **Fix:** Python `logging` config writes to `/var/log/nexoria/` with rotation.
  Already partially done — `logs/gateway_crash.log` exists but not centralized.

### 9. Auto-restart
- **Now:** Nothing watches the services. Crash = manual restart.
- **Fix:** systemd `Restart=always` directive or Docker restart policy. FastAPI already
  exposes `/health` endpoint (`main.py` line ~894).

---

## Widget + Chat URL Routing Issue

The chat widget embedded on hotel websites calls back to the server. This breaks in production because:

- Widget is hardcoded to `http://localhost:8011` or `http://localhost:8080` (dev URLs)
- In production the server has a real domain — the widget URL needs to match
- Without HTTPS the widget won't load on HTTPS hotel websites (mixed content block)

**What needs to happen:**
1. Widget base URL must be set to the production domain e.g. `https://chat.yourcompany.com`
2. FastAPI CORS must allow the hotel website domains that embed the widget
3. Chat endpoint `/api/chat` must be reachable via HTTPS

---

## Files to Change When Executing

| File | Change Needed |
|---|---|
| `main.py` | Remove subprocess management for Vite + scraper; those run independently |
| `admin_ui/vite.config.ts` | Proxy config is dev-only; Nginx handles routing in prod |
| `.env` (server) | `APP_ENV=production`, real `DATABASE_URL`, real domain settings |
| `api/routes/admin.py` | Remove `ADMIN_DEV_URL` proxy logic (only needed in dev) |
| `main.py` | Remove SQLite override for scraper (lines 244–248) once DB is reachable |
| New: `nginx.conf` | Nginx config for routing frontend + backend + scraper |
| New: `nexoria.service` | systemd unit file for FastAPI |
| New: `kb-scraper.service` | systemd unit file for KB scraper |

---

## Recommended Execution Order

1. Fix DB reachability (unblocks everything else)
2. Run `npm run build` and verify static files are served correctly
3. Write `nginx.conf` and test locally
4. Fix widget URL and CORS for production domain
5. Add HTTPS via Certbot
6. Write systemd service files
7. Lock down secrets

---

*Created: 2026-04-21 | Project: kebo / NexOria*
