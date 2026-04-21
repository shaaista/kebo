# Docker + Nginx Deployment Plan

**Date:** 2026-04-21  
**Status:** Planning — awaiting developer approval before any server changes

---

## Current Problems (Plain English)

| Problem | What's happening | Impact |
|---|---|---|
| White screen at deployed URL | `npm run build` never runs in container — React JS files don't exist | Site unusable |
| MIME type error in browser console | FastAPI returns HTML when browser requests JS files | React never boots |
| Port conflict | Both `kebo-app` and `kebo-ui` try to use host port 8000 | Container startup fails |
| No Nginx | Nothing routes `/admin/` to React or `/api/` to FastAPI | Traffic goes to wrong service |
| KB scraper never starts | Dockerfile uses `uvicorn main:app` not `python main.py` | No web crawling in production |
| `docker-compose down -v` wipes data | `-v` flag deletes named volumes (logs, state) on every deploy | Logs lost every deployment |
| Scraper using SQLite | `main.py` overrides `DATABASE_URL` to force SQLite | Scraper data isolated, not in MySQL |

---

## The Fix Plan (Step by Step)

### Step 1 — Make the frontend build
- Create `admin_ui/Dockerfile`
- It installs npm dependencies and runs `npm run build`
- Built React files go into Nginx's serving directory

### Step 2 — Add Nginx (traffic router)
- Add Nginx container to `docker-compose.yml`
- Write `nginx.conf` with routing rules:
  - `/admin/` → serve React static files
  - `/api/` → proxy to FastAPI (port 8011 internally)
  - `/kb-scraper/` → proxy to KB scraper (port 8501 internally)

### Step 3 — Fix port conflict
- Only Nginx exposed on port 8000 to the outside world
- FastAPI stays on internal port 8011 (not internet-facing)
- KB scraper stays on internal port 8501 (not internet-facing)

### Step 4 — Fix KB scraper startup
- Add dedicated scraper container in `docker-compose.yml`
- Remove SQLite override so scraper uses MySQL like the rest of the app

### Step 5 — Fix deploy command
- Change `docker-compose down -v` → `docker-compose down` on the server
- Keeps logs and state volumes between deploys

### Step 6 — Fix log visibility (optional)
- Mount logs to a host path so logs are visible in MobaXterm

---

## Architecture After Fix

```
Internet
    │
    ▼
Port 8000 ── Nginx (traffic cop)
                ├── /admin/*        → React static files (built by admin_ui/Dockerfile)
                ├── /api/*          → FastAPI backend (internal port 8011)
                └── /kb-scraper/*  → KB scraper (internal port 8501)
```

**Benefit of separate containers:** If FastAPI crashes, Nginx still serves the React frontend. If the scraper crashes, chat and admin still work. Docker `restart: unless-stopped` auto-restarts crashed containers.

---

## What Goes to GitHub vs MobaXterm

| File | Where it goes | Why |
|---|---|---|
| Python/React code changes | GitHub → `git pull` on server | Normal code flow |
| `Dockerfile` (root) | MobaXterm directly (already in `.gitignore`) | Server-specific |
| `docker-compose.yml` | MobaXterm directly (already in `.gitignore`) | Server-specific |
| `admin_ui/Dockerfile` | GitHub (new file, needs to be committed) | Part of the build |
| `nginx.conf` | MobaXterm directly | Server-specific |

---

## Why NOT Merge KB Scraper into Backend (Yet)

| Risk | Detail |
|---|---|
| Route collision | Both apps use `/api/` prefix — routes overwrite each other |
| Two database setups | Each has its own `Base`, `engine`, `async_session` — need careful unification |
| Two `main.py` files | 900+ line files that need to be manually merged |
| Background job wiring | Scraper's `worker.py` + `supervisor.py` need to be integrated into main app startup |
| Bot disruption risk | If route prefix collision happens, hotel chat breaks |

**Recommendation:** Do the merge after production is stable. Nginx proxying `/kb-scraper/` achieves the same user-facing result (single port 8000) with zero code risk.

---

## Access Needed Before Starting

| Requirement | Status |
|---|---|
| MobaXterm SSH to server | Have it |
| GitHub push access | Have it |
| MySQL VPN connection from server | Confirm with developer |
| AWS console login | Not needed |
| Developer approval for docker-compose changes | Pending |

---

## Message to Send Developer

> Hey, I want to make the kebo deployment production-ready. Here's the plan — wanted to run it by you before making any server changes.
>
> **What we're doing:**
> 1. Adding a `Dockerfile` inside `admin_ui/` — runs `npm run build` so React frontend gets built in the container (current white screen is because the build never happens)
> 2. Adding an Nginx container to `docker-compose.yml` — sits on port 8000 and routes: `/admin/` to React files, `/api/` to FastAPI backend
> 3. FastAPI backend moves to internal port 8011 (not exposed to internet directly)
> 4. KB scraper runs as its own container on internal port 8501 (not exposed either)
> 5. Only Nginx is exposed on port 8000 — everything else internal
>
> **What I need from you:**
> - Is anything on the server already running on port 8000?
> - Is `172.16.5.32:3306` (MySQL) reachable from the server without VPN, or does the server need VPN access too?
> - Okay with me updating `Dockerfile` and `docker-compose.yml` directly via MobaXterm? These won't go to GitHub.
> - Current deploy command is `docker-compose down -v` — the `-v` deletes logs every deploy. Can I change it to `docker-compose down` (without `-v`)?
