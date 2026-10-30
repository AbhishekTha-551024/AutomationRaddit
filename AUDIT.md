# System Audit: Social Media Post Scheduler

**Audit Date**: October 2026  
**Auditor**: Senior Backend Architect  
**Codebase Version**: 0.5.0 (FastAPI + SQLAlchemy 2.x + SQLite + APScheduler + Jinja2)

---

## 1. Executive Summary

The project is an active, functioning social media scheduler that successfully schedules, retries, and publishes posts to **Telegram**, **Discord**, and **Meta Threads**. It features:
- A clean atomic-claim dispatch loop preventing double-posting.
- An encrypted token vault for Threads using Fernet.
- An IST-aware web UI with a composer, calendar, timeline logs, and light/dark theme.
- 40+ unit and integration tests verifying adapters and routes.

While functionally solid, the codebase has reached the limit of its initial monolithic prototype structure. Business logic is intertwined with HTTP route handlers, adapters are hardcoded across templates and scripts, database queries are executed directly in views, and scheduling is coupled to the web server process. 

This audit provides a comprehensive breakdown of the current system architecture, identified technical debt, preserved strengths, and test coverage.

---

## 2. Current Folder Structure & File Responsibilities

```
Automation/
├── .env                  # Environment variables & secrets (never committed)
├── .env.example          # Template for required environment variables
├── .gitignore            # Git exclusion rules
├── DESIGN.md             # UI/UX design specifications, tokens, and wireframes
├── PLAN.md               # Phased roadmap history (Phases 1-4 + Threads)
├── requirements.txt      # Python dependencies
├── scheduler.db          # Local SQLite database
├── app/
│   ├── __init__.py       # Package marker
│   ├── config.py         # Application settings via pydantic-settings
│   ├── database.py       # Engine, SessionLocal, Base, and startup migration check
│   ├── main.py           # FastAPI application factory and lifespan hooks
│   ├── adapters/         # Platform publisher implementations
│   │   ├── __init__.py
│   │   ├── base.py       # BaseAdapter abstract class & PublishResult dataclass
│   │   ├── discord.py    # Discord webhook publisher with 2000-char truncation
│   │   ├── registry.py   # Platform discovery map (SUPPORTED_PLATFORMS)
│   │   ├── telegram.py   # Telegram Bot API sendMessage publisher
│   │   └── threads.py    # Meta Threads 2-step publisher with thread chaining
│   ├── api/              # HTTP routers (JSON endpoints & Web HTML views)
│   │   ├── __init__.py
│   │   ├── dev.py        # Development test endpoints (/dev/test-*, /dev/threads-account)
│   │   ├── logs.py       # GET /logs audit trail endpoint
│   │   ├── posts.py      # POST /posts, GET /posts, GET /posts/{id}
│   │   ├── stats.py      # GET /stats status aggregation
│   │   ├── targets.py    # GET /targets/{id}/logs, POST /targets/{id}/retry
│   │   └── web.py        # HTML views: Dashboard (/), Composer (/posts/new), Calendar (/calendar), Detail (/posts/{id}/view)
│   ├── models/           # SQLAlchemy 2.0 ORM Declarative Models
│   │   ├── __init__.py   # Model registry exports
│   │   ├── account.py    # Account model (encrypted credentials, token expiry)
│   │   ├── log.py        # Log model (audit logs linked to targets)
│   │   ├── post.py       # Post model (content, title, media_path)
│   │   └── target.py     # PostTarget model (platform, status, retry state)
│   ├── scheduler/        # Background execution
│   │   ├── __init__.py
│   │   └── scheduler.py  # APScheduler polling loop & crash recovery
│   ├── schemas/          # Intended for Pydantic schemas (currently empty)
│   │   └── __init__.py
│   ├── services/         # Core business logic services
│   │   ├── __init__.py
│   │   ├── crypto_service.py         # Fernet encryption & key derivation
│   │   ├── publisher.py              # run_due_posts dispatch & retry orchestrator
│   │   └── threads_token_service.py  # Threads token exchange, refresh, & /me profile
│   ├── static/           # Static frontend assets
│   │   ├── css/
│   │   │   └── styles.css # Monolithic stylesheet (970+ lines)
│   │   └── js/
│   │       └── app.js     # Monolithic client JS (420+ lines)
│   └── templates/        # Jinja2 HTML templates
│       ├── base.html        # App shell, sidebar, mobile nav, theme toggle
│       ├── calendar.html    # Monthly calendar grid with day drawer
│       ├── composer.html    # Post composer form & live preview
│       ├── dashboard.html   # Main dashboard (Up Next, Failed, Recent Activity)
│       └── post_detail.html # Post inspection, target breakdown & execution timeline
└── tests/                # Test suite
    ├── __init__.py
    ├── test_discord_adapter.py # Discord adapter unit tests
    ├── test_phase1.py          # Database & configuration smoke tests
    ├── test_phase2.py          # Telegram adapter unit tests
    ├── test_phase3.py          # Timezone parsing & scheduling API tests
    ├── test_phase4.py          # Backoff retries, log endpoints & recovery tests
    ├── test_threads_adapter.py # Threads adapter, crypto & thread chain tests
    └── test_web_views.py       # HTML template rendering tests
```

---

## 3. Identified Problems & Technical Debt

### 3.1 Architecture & Layering Violations
| Issue | Location | Impact |
| :--- | :--- | :--- |
| **Business logic in API routes** | `app/api/targets.py:80-95` | `retry_failed_target` directly resets attempts, recalculates UTC times, and inserts audit logs inside the route function. Cannot be reused by CLI or background jobs. |
| **Calendar math in Web router** | `app/api/web.py:174-245` | Date window boundary math, month day chunking, and post grouping by date string are written directly in the route handler. |
| **Database queries in routes** | `app/api/posts.py`, `app/api/stats.py`, `app/api/web.py` | Direct SQLAlchemy queries (`select(PostTarget)`, `db.execute()`) live inside route controllers. No repository layer exists. |
| **Empty schemas directory** | `app/schemas/` | An empty `schemas` directory exists, but Pydantic request/response models are defined inline inside `app/api/posts.py` and `app/api/targets.py`. |
| **Cross-layer coupling** | `app/services/publisher.py:13` | The service layer imports `format_to_ist` from `app.api.posts`. Services should never depend on API presentation layers. |

### 3.2 Coupling & Inflexible Platform Structure
- **Hardcoded Platform Registry**: Adding a new platform requires modifying `app/adapters/registry.py`, creating an adapter file, updating `composer.html` (SVG icon branching), and modifying `app.js` (character limits).
- **Hardcoded Limits in Frontend**: `PLATFORM_LIMITS` (`telegram: 4096, discord: 2000, threads: 500`) is hardcoded in `app.js`. Platforms cannot dynamically declare their limits, supported media, or formatting features.
- **Single Account per Platform in `.env`**: Telegram and Discord read single tokens directly from `.env`. There is no way for a user to schedule posts to two different Telegram channels or two different Discord servers without editing `.env`.
- **Inconsistent Account Models**: Threads stores credentials in the `accounts` database table, while Telegram and Discord bypass the `accounts` table entirely.

### 3.3 Large Monolithic Files
- **`app/static/css/styles.css` (970+ lines)**: Contains color variables, reset rules, typography, sidebar layouts, calendar grids, badges, form inputs, preview bubbles, and responsive breakpoints in a single file. Hard to maintain or theme modularly.
- **`app/static/js/app.js` (420+ lines)**: Handles theme toggles, toast notifications, composer inputs, presets, AJAX submissions, character counters, thread splitting, retries, and polling all in one file.
- **`app/adapters/threads.py` (320+ lines)**: Handles 500-char validation, thread chain parsing, container creation, container polling, container publishing, token recovery, rate limiting sleeps, DB state commits, and error mapping in one class.

### 3.4 Duplication of Logic
- **Thread Chain Splitting**: The algorithm to split post bodies on lines containing `---` is implemented in Python in `app/adapters/threads.py` (`split_thread_parts`) and duplicated in JavaScript in `app/static/js/app.js` (`splitThreadPartsJs`). Any change to formatting requires remembering to edit both.
- **Status Counts**: Target status aggregation is written once in `app/api/stats.py` (`get_target_stats`) and reimplemented from scratch in `app/api/web.py` (`dashboard_view`).
- **Timezone Conversions**: Formatting UTC to IST strings (`%Y-%m-%d %H:%M:%S IST`) is implemented in `app/api/posts.py` and reused informally or reimplemented in template contexts.

### 3.5 Database & Migration Risks
- **No Migration Tool (Alembic)**: Database schema setup relies on `Base.metadata.create_all(bind=engine)`. In SQLite, `create_all` does not add columns to existing tables.
- **Ad-hoc `migrate_db_columns()`**: `app/database.py` contains a custom helper that runs `ALTER TABLE post_targets ADD COLUMN publish_state TEXT;`. This does not scale as indexes, column types, or new tables evolve.
- **SQLite Concurrency Contention**: SQLite locks the database file on writes. When the APScheduler background thread claims jobs while a user creates posts or views the dashboard, `sqlite3.OperationalError: database is locked` can occur under concurrent load.
- **Missing Indexes**: `post_targets` lacks composite indexes on `(status, scheduled_at, next_retry_at)`, causing full table scans during high-frequency polling.

### 3.6 Background Worker & Concurrency
- **Scheduler Coupled to Web Server**: APScheduler runs as an in-process thread inside the FastAPI Uvicorn process (`app/main.py`). If the web server crashes or restarts, the scheduler stops. If multiple Uvicorn worker processes are spawned (`uvicorn --workers 4`), multiple schedulers will run simultaneously and fight over the database.
- **Blocking Sleeps in Worker**: `ThreadsAdapter` calls `time.sleep(2.0)` between thread parts and `time.sleep(1.0)` inside container polling loops. This blocks the scheduler thread during dispatch.
- **Stuck Job Timeout**: If an adapter hangs indefinitely or the process is killed midway through publishing, a target remains in `status='processing'` until the server is rebooted (handled only on startup by `recover_orphaned_jobs`).

### 3.7 Security & Secrets Handling
- **`.env` Reloading Lag**: `pydantic-settings` loads `.env` once at application startup. Modifying `.env` requires restarting the entire server process.
- **Plaintext Webhook URLs**: `DISCORD_WEBHOOK_URL` contains sensitive webhook tokens in `.env` rather than being stored encrypted in the database.
- **Missing Auth Layer**: The application has no user authentication or session management; anyone with network access to port 8000 can publish, retry, or delete posts.

---

## 4. What Works Well & Must Be Preserved

1. **Atomic Claiming Mechanism**:  
   `UPDATE post_targets SET status='processing' WHERE id=:id AND status='pending'` guarantees that two workers will never double-publish the same target.
2. **Standardized Adapter Interface**:  
   `BaseAdapter` returning `PublishResult(success, external_post_id, error_message, retryable)` provides a reliable contract across wildly different social networks.
3. **Transient vs Permanent Error Classification**:  
   Permanent client errors (400, 401, 403, 404) fail immediately without useless retries; transient errors (429, 5xx, timeouts) back off intelligently.
4. **Incremental Checkpointing (`publish_state`)**:  
   Thread chains record published part IDs. Retries resume from the failed part rather than duplicating earlier parts.
5. **Secret Redaction**:  
   `mask_secret()` sanitizes exceptions and HTTP errors so API tokens never leak into the database logs or API responses.
6. **Clean, Distraction-Free UI**:  
   The Jinja2 dashboard is fast, calm, responsive, and displays all timestamps clearly in IST.

---

## 5. Current Test Coverage Breakdown

| Test File | Target Under Test | Coverage Assessment |
| :--- | :--- | :--- |
| `tests/test_phase1.py` | Models & Config | Verifies table names and TargetStatus enums. Good smoke test. |
| `tests/test_phase2.py` | Telegram Adapter | Verifies missing token, success, and HTTP error handling. |
| `tests/test_phase3.py` | Posts API & Scheduling | Verifies IST/UTC conversion and target scheduling. |
| `tests/test_phase4.py` | Retries & Audit Logs | Verifies backoff delays, permanent errors, manual retry, and log queries. |
| `tests/test_discord_adapter.py` | Discord Adapter | Verifies 2000-char truncation, success, 404 permanent error, 429 retryable. |
| `tests/test_threads_adapter.py` | Threads Adapter & Crypto | Verifies Fernet encryption, 500-char limit, single post, thread chain, and idempotency. |
| `tests/test_web_views.py` | Jinja2 Web Views | Verifies HTTP 200 responses for Dashboard, Composer, Calendar, and Detail views. |

### Critical Testing Gaps to Address
- **No Concurrency Tests**: No tests simulate simultaneous claim attempts by two workers on the same target.
- **No Worker Separation Tests**: APScheduler startup and crash recovery have no dedicated integration tests.
- **No Calendar Edge-Case Tests**: Leap years, month boundaries (e.g. Feb 28 to March 1), and empty months are untested.
- **Ad-Hoc Test Execution**: Tests do not use standard pytest fixtures or configuration files (`pytest.ini`).

