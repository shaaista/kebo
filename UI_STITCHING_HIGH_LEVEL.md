# UI Stitching High-Level Document (Current State)

Generated from the current repository state at:
- Date: 2026-04-15
- Branch: `main`
- Commit: `cbf5383`

This document is based on code inspection of this folder only (no assumptions from earlier iterations).

## 1) Objective Completed

The project is now stitched as a React-based admin + chat UI while preserving backend APIs and business logic.

- Admin UI is React SPA, served at `/admin`.
- Chat test/widget UI is React page, served at `/chat`.
- Backend remains FastAPI and continues to own all API behavior, orchestration, ticketing, phases, services, and RAG.

## 2) Runtime Architecture

### Backend serving model
- FastAPI serves:
  - `/admin` -> React `index.html` build
  - `/chat` -> React `chat.html` build
  - `/static` -> static assets (including widget loader script)
- Key files:
  - `main.py`
  - `api/routes/admin.py`

### Frontend build model
- `admin_ui` uses Vite with **two entries**:
  - `index.html` (admin app)
  - `chat.html` (chat harness/widget page)
- Vite base path is `/admin/`.
- Key file:
  - `admin_ui/vite.config.ts`

### Note on HTML
- Legacy server templates for chat/admin are not present (`templates` folder is currently empty).
- Two minimal HTML entry files still exist in `admin_ui` (`index.html`, `chat.html`) because Vite requires entry HTML shells.
- Actual UI implementation is React (`.tsx`).

## 3) Tech Stack Used

## Backend
- Python, FastAPI, Uvicorn
- SQLAlchemy (async), SQLite/MySQL support
- Pydantic

## Frontend
- React 18 + TypeScript
- Vite 5
- React Router
- TanStack Query
- Tailwind CSS
- Radix/shadcn-style component stack
- Framer Motion + Lucide icons

## Testing/Tooling Present
- Vitest / Testing Library (frontend deps present)
- Playwright config present in `admin_ui`

## 4) What Was Stitched (Feature-Level)

## A) Admin React SPA stitched to backend

- Router moved to React with `/admin` basename.
- Sidebar + page routes wired:
  - Dashboard, Crawl, Content, Forms
  - Bot Training
  - Staff, Departments, Notifications, Escalation Matrix
- Key files:
  - `admin_ui/src/App.tsx`
  - `admin_ui/src/components/AppSidebar.tsx`

## B) Bot Training section integrated with existing backend APIs

`BotTraining.tsx` and tab components call existing `/admin/api/*` endpoints; logic remains backend-driven.

- Setup/Wizard:
  - Business, prompts, knowledge, UI/channel settings
  - Template apply, prompt template apply
  - Import/export config
- RAG tab:
  - status, query, reindex, upload, files, async jobs
- Phases tab:
  - phase-aware service mapping
  - prebuilt service install
  - service description/ticketing condition suggestion
  - prompt regenerate/save
  - KB extraction support
- Services tab:
  - edit existing service config (phase + ticketing fields)
- FAQ tab:
  - CRUD for FAQ bank
- Evaluation tab:
  - summary + recent events
- Escalation tab:
  - escalation policy save/load
- Advanced tab:
  - DB status, sync, raw config import/export

Key files:
- `admin_ui/src/pages/app/BotTraining.tsx`
- `admin_ui/src/components/bot-training/*`
- `admin_ui/src/lib/adminApi.ts`

## C) Property-scoped admin calls preserved

- Frontend helper injects `x-hotel-code` header on every admin request.
- Property code is persisted in localStorage and reused across tabs/pages.
- Key file:
  - `admin_ui/src/lib/adminApi.ts`

## D) React chat harness + widget flow integrated

`/chat` is React (`ChatHarness.tsx`) and supports:
- Hotel/property selection (`/api/chat/properties`)
- Phase selection + test profiles (`/api/chat/test-profiles`)
- Booking linking and creation (`/admin/api/bookings`)
- Chat send (`/api/chat/message`)
- Inline form submit (`/api/chat/form-submit`)
- Contextual suggestions (`/api/chat/suggestions`)
- Session history/reset (`/api/chat/session/...`)
- Local ticket list (`/admin/api/tickets`)

Behavior aligned with backend:
- Welcome text is loaded from admin business config (`/admin/api/config/onboarding/business`, field `welcome_message`).
- Suggested chips come from backend output (`suggested_actions` + suggestions endpoint), not fixed hardcoded chips.
- Assistant response markdown formatting is rendered on UI.
- Inline form supports `date`, `time`, `datetime-local/datetime`, `select/dropdown`, phone field with country code.

Key files:
- `admin_ui/src/pages/ChatHarness.tsx`
- `admin_ui/src/chat-main.tsx`

## E) Embeddable widget loader present

- Script: `/static/embed/kebo-widget-loader.js`
- Builds iframe URL to `/chat?embed=1...`
- Supports theme, position, size, bot name, hotel code, session id, API base hints
- Exposes `window.KeboWidget` (`open`, `close`, `toggle`, `destroy`)

## 5) Backend Compatibility and Non-Regression Anchors

- Backend routers still handle core functionality:
  - Chat: `api/routes/chat.py`
  - Admin config/training APIs: `api/routes/admin.py`
- FastAPI now prints corrected startup links:
  - `Server`, `API Docs`, `Test Chat`, `Admin Portal`
- Request-level terminal logs added for API traffic:
  - `[API] ...`
  - `[ADMIN_API] ...`
- Uvicorn access logging explicitly enabled.

Key file:
- `main.py`

## 6) Operational Model

This repository currently runs best as a **single backend server** for integrated behavior:

1. Build frontend bundle:
   - `cd admin_ui`
   - `npm run build`
2. Run backend:
   - `python main.py`
3. Open:
   - `http://localhost:8000/admin`
   - `http://localhost:8000/chat`

Important:
- Root folder does not contain `package.json`; frontend npm commands must run inside `admin_ui`.

## 7) Verification Performed for This Document

Executed during this audit:
- `npm.cmd run build` (in `admin_ui`) -> success, generated `admin_ui/dist/index.html` and `admin_ui/dist/chat.html`
- `python -m py_compile main.py` -> success

## 8) Current Known Constraints

- `templates/` exists but is empty (no legacy server HTML templates in use for admin/chat).
- Vite build reports large chunk warning for `main-*.js` (optimization opportunity: code splitting/manual chunks).
- Entry HTML files (`admin_ui/index.html`, `admin_ui/chat.html`) remain required by Vite build pipeline.
