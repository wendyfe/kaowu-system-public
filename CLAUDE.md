# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run dev server (from project root, requires Python 3.10+)
cd app && uvicorn main:app --reload --port 8000

# Run with Docker
docker compose up -d --build

# DB location (auto-created)
app/db/kaowu.db
```

## Dependencies

All in `requirements.txt`: FastAPI, Uvicorn, SQLAlchemy (SQLite), Jinja2, pandas/openpyxl (Excel export), itsdangerous (sessions), python-multipart.

Frontend CDN dependencies: Bootstrap 5.3.0 (CSS+JS), Flatpickr (date picker + zh locale). Loaded directly in HTML files.

## Project Structure

```
app/
  main.py              # Single-file backend: FastAPI app, DB models, routes, middleware, all logic
  static/
    admin.html          # Admin panel: publish/edit/delete/toggle recruits, export Excel
    admin_login.html    # Admin login page
    student.html        # Student registration, query, cancel registration
    style.css           # Shared styles
  templates/
    index.html          # Landing page (Jinja2 template, entry point for both portals)
docker-compose.yml      # Production deployment
Dockerfile              # Container build (python:3.10-slim)
```

## Architecture & Key Patterns

### Backend (single file, ~635 lines)

- **FastAPI** app with 3 SQLAlchemy models: `Recruitment`, `Registration`, `VerifyCode`
- **SQLite** database, auto-created at `app/db/kaowu.db`
- **Admin auth**: Cookie-based session via `itsdangerous.URLSafeTimedSerializer`, 1-hour expiry, httpOnly cookie `kaowu_admin`
- **CSRF protection**: Non-httpOnly cookie `kaowu_csrf` + `X-CSRF-Token` header, enforced on all admin write endpoints
- **Rate limiting**: In-memory dict (`_RATE_LIMITS`) keyed by IP or action
- **Client IP**: Extracts from `X-Forwarded-For` → `X-Real-IP` → `request.client.host`
- **Email**: SMTP_SSL via QQ mail for cancel-registration verification codes, sent via BackgroundTasks
- **Beijing timezone**: All timestamps use `Asia/Shanghai` via `zoneinfo`

### Frontend (static HTML + Bootstrap 5)

- All pages are static HTML served via `FileResponse`, except index.html (Jinja2 template)
- Bootstrap 5.3.0 for layout, modals, toasts; Flatpickr for date/time picking
- XSS prevention: all dynamic content rendered via `textContent`, never `innerHTML`
- CSRF token in admin pages extracted from `document.cookie` (cookie is non-httpOnly)

### API Endpoints

| Method | Path | Auth | CSRF | Description |
|--------|------|------|------|-------------|
| GET | `/` | - | - | Landing page (Jinja2) |
| GET | `/student` | - | - | Student portal |
| GET | `/admin/login` | - | - | Admin login page |
| GET | `/admin` | Cookie | - | Admin dashboard |
| POST | `/api/admin/login` | - | - | Login (rate: 5/60s) |
| GET | `/api/admin/logout` | - | - | Logout + redirect |
| POST | `/api/recruit/add` | Cookie | Yes | Publish recruit (rate: 10/60s) |
| PUT | `/api/recruit/{id}` | Cookie | Yes | Edit recruit |
| POST | `/api/recruit/{id}/toggle` | Cookie | Yes | Toggle active/closed |
| DELETE | `/api/recruit/{id}` | Cookie | Yes | Delete + cascade registrations |
| GET | `/api/recruit/admin-list` | Cookie | - | All recruits (admin) |
| GET | `/api/recruit/list` | - | - | Active recruits (student) |
| POST | `/api/reg` | - | - | Register (rate: 10/60s) |
| POST | `/api/my-registrations` | - | - | Query my registrations |
| POST | `/api/send-verify-code` | - | - | Send cancel code (rate: 3/300s) |
| POST | `/api/cancel-reg` | - | - | Cancel via code (rate: 5/300s) |
| GET | `/api/export/{id}` | Cookie | - | Export registrations as .xlsx |

### Known Technical Notes

- `_cleanup_rate_limits()` is defined but never called; rate-limit dict grows unbounded
- No `load_dotenv()` — env vars must be set in the OS environment or docker-compose
- No database foreign key constraints; cascade deletion is handled in application code
- Session cookie has fixed 1-hour expiry with no sliding renewal
- .env file was previously committed to git (credentials may need rotation if repo is public)
