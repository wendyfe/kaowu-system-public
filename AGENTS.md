# AGENTS.md

This file gives Codex and other AI coding agents the current working map for this repository.

## Commands

```bash
# Run dev server from the project root
cd app && uvicorn main:app --reload --port 8000

# Run with Docker
docker compose up -d --build

# Local SQLite database, auto-created at runtime
app/db/kaowu.db
```

## Dependencies

Python dependencies are pinned in `requirements.txt`: FastAPI, Uvicorn, SQLAlchemy, Jinja2, pandas/openpyxl/xlrd, reportlab, dbfread, itsdangerous, and python-multipart.

Frontend dependencies are loaded from CDNs in the HTML files: Bootstrap 5.3, Flatpickr, and page-specific browser libraries where used.

## Project Structure

```text
app/
  main.py              # FastAPI app, SQLAlchemy models, migrations, routes, auth, core business logic
  tool_processors.py   # Admin tool processors: invigilator assignment, seat labels, workbook processing
  static/
    admin.html          # Admin panel: recruitment, classrooms, grouping, training, acceptance, tools
    admin_login.html    # Admin login page
    student.html        # Student portal: registration, query, cancellation, task/recovery workflows
    training.html       # Student training video page with progress tracking and embedded questions
    style.css           # Shared styles
  templates/
    index.html          # Landing page rendered through Jinja2
docker-compose.yml      # Production deployment
Dockerfile              # Container build
scripts/
  backup_to_github.sh   # VPS SQLite encrypted backup helper
```

## Current Architecture

- Backend is still intentionally concentrated in `app/main.py`, now around 4,700 lines.
- Tool-heavy Excel/PDF/data logic lives in `app/tool_processors.py` to keep some large processors out of the route file.
- SQLite is used through SQLAlchemy. `DB_DIR` can override the database directory; otherwise it defaults to `app/db`.
- Local `.env` files are read by `load_env_file()` from either `app/.env` or project-root `.env`, without overriding existing environment variables. Docker uses `env_file: .env`.
- Static pages are served with `FileResponse`; only `/` uses `Jinja2Templates`.
- Beijing time is the system business timezone via `Asia/Shanghai`.

## Main Domains

- Recruitment publishing, editing, closing, deletion, registration, query, cancellation by email verification code, and Excel export.
- Classroom/building management, classroom import, recruitment classroom selection, and generated room numbers.
- Grouping workflows: general supervisor, building supervisors, automatic grouping, manual grouping, member assignment, classroom assignment, and grouping-result export.
- Setup, acceptance, sealing, recovery, and student task progress workflows.
- Student training video workflow with cookie-based training login, video progress, completion threshold, and timed questions.
- Admin training overview and training question CRUD.
- Admin tools unlocked by a separate PIN: invigilator assignment, seat-label PDF generation/precheck/template, CET data tools, graduate data import, workbook merge, and pass-rate analysis.

## Security And State

- Admin auth uses `itsdangerous.URLSafeTimedSerializer` and the `kaowu_admin` httpOnly cookie.
- CSRF uses a non-httpOnly `kaowu_csrf` cookie plus `X-CSRF-Token` on admin write endpoints.
- Admin tool access uses `KAOWU_TOOLS_PIN` and a separate `kaowu_tools` cookie.
- Training access uses the `kaowu_training` cookie.
- Rate limiting is in-memory through `_RATE_LIMITS`; `_cleanup_rate_limits()` is now called from `rate_limit()`.
- Client IP is derived from `X-Forwarded-For`, then `X-Real-IP`, then `request.client.host`.
- Email verification uses SMTP_SSL with `SMTP_USER`, `SMTP_PASS`, `SMTP_HOST`, and `SMTP_PORT`.

## Important Environment Variables

- `KAOWU_ADMIN_USERNAME`, `KAOWU_ADMIN_PASSWORD`, `KAOWU_SECRET_KEY`
- `DB_DIR`
- `SMTP_USER`, `SMTP_PASS`, `SMTP_HOST`, `SMTP_PORT`
- `KAOWU_TOOLS_PIN`, `KAOWU_TOOLS_UNLOCK_MAX_AGE`
- `TRAINING_VIDEO_PATH`, `TRAINING_PUBLIC_VIDEO_URL`, `TRAINING_VIDEO_DURATION_SECONDS`, `TRAINING_COOKIE_MAX_AGE`

## Files That Should Stay Local

- `.env`
- `app/db/`
- `.claude/`
- `.superpowers/`
- `tmp_seat_label_tests/`
- `docs/考务系统VPS备份与恢复手工操作手册.md`

## Notes For Future Changes

- Prefer small, scoped edits in `app/main.py`; it contains many route families and implicit cross-feature assumptions.
- For Excel/PDF/tool behavior, check `app/tool_processors.py` before adding logic to routes.
- Keep dynamic frontend rendering on `textContent`/DOM APIs rather than `innerHTML` unless the content is fully controlled.
- The production Docker image copies `app/` into the image; static frontend edits require rebuilding the image unless a deployment adds a static mount.
- The `.env` file was previously committed historically; credentials should be treated as rotated-only if this repository is public.
