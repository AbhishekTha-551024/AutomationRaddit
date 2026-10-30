# Refactoring Roadmap: Social Media Post Scheduler

**Document Version**: 1.0  
**Status**: Step-by-Step Implementation Roadmap  
**Author**: Senior Backend Architect  

---

## 1. Guiding Principles for the Refactoring

1. **Zero Downtime / Zero Regressions**: The application must remain completely functional after **every single stage**.
2. **Small Atomic Commits**: Each stage is an isolated pull request or commit that can be tested, reviewed, and rolled back independently.
3. **Data Preservation**: Existing SQLite data (posts, targets, accounts, execution logs) must never be lost or reset.
4. **Safety Net First**: Comprehensive automated tests must be established before moving or refactoring application code.

---

## 2. Step-by-Step Migration Roadmap

```
Stage 1: Safety Net (Pytest & Baseline Tests)
   │
   ▼
Stage 2: Alembic Migrations & Schema Baselining
   │
   ▼
Stage 3: Repository Layer Extraction
   │
   ▼
Stage 4: Service Layer Refactoring & Schema Isolation
   │
   ▼
Stage 5: Platform Plugin System & Dynamic Capabilities
   │
   ▼
Stage 6: Multi-Account Architecture & Credential Vault
   │
   ▼
Stage 7: Worker Separation & Job Queue Abstraction
   │
   ▼
Stage 8: Media Pipeline & Storage Abstraction
   │
   ▼
Stage 9: Observability, Structured Logging & Health Probes
   │
   ▼
Stage 10: Jinja2 Templates & CSS Modularization
   │
   ▼
Stage 11: Developer Tooling, Docker & CI/CD
```

---

### Stage 1: Safety Net (Pytest & Baseline Tests)
* **Goal**: Install `pytest` and `pytest-mock`, consolidate existing unit tests into standard pytest format, and add comprehensive integration tests around existing scheduling and retry behavior.
* **Files Created / Moved**:
  * `pytest.ini` (pytest configuration)
  * `conftest.py` (fixtures for test DB, mock clients, test posts)
  * `tests/test_scheduler_integration.py` (simulates full scheduler cycle)
  * `requirements.txt` (add `pytest>=8.0.0`, `pytest-mock>=3.12.0`)
* **Risk Level**: **Low** (No application code changes)
* **How to Verify Nothing Broke**:
  ```powershell
  # 1. Run all tests via pytest
  .\venv\Scripts\pytest -v

  # 2. Verify server boots and responds
  .\venv\Scripts\python -c "import httpx; assert httpx.get('http://127.0.0.1:8000/health').status_code == 200"
  ```
* **Git Commit Message**:  
  `test: setup pytest runner and add baseline scheduler integration tests`

---

### Stage 2: Database Migrations (Alembic)
* **Goal**: Replace `Base.metadata.create_all()` and custom `migrate_db_columns()` with Alembic version-controlled migrations without wiping existing data.
* **Files Created / Moved**:
  * `alembic.ini` (Alembic configuration)
  * `alembic/env.py` (database connection & model metadata hook)
  * `alembic/versions/0001_initial_baseline.py` (captures existing posts, targets, accounts, logs)
  * `requirements.txt` (add `alembic>=1.13.0`)
  * `app/database.py` (remove `migrate_db_columns()`)
  * `app/main.py` (remove `create_all()`, execute `alembic upgrade head` on startup)
* **Risk Level**: **Medium** (Database schema management change)
* **How to Verify Nothing Broke**:
  ```powershell
  # 1. Run Alembic upgrade on existing database
  .\venv\Scripts\alembic upgrade head

  # 2. Check current Alembic revision matches baseline
  .\venv\Scripts\alembic current

  # 3. Query existing posts to ensure data intact
  .\venv\Scripts\python -c "from app.database import SessionLocal; from app.models import Post; db = SessionLocal(); print('Total existing posts:', db.query(Post).count()); db.close()"
  ```
* **Git Commit Message**:  
  `db: introduce alembic migrations and baseline existing schema`

---

### Stage 3: Repository Layer Extraction
* **Goal**: Create dedicated repository classes in `app/repositories/` to encapsulate all SQLAlchemy queries. Remove direct SQL queries from API routes.
* **Files Created / Moved**:
  * `app/repositories/base.py` (BaseRepository with generic CRUD)
  * `app/repositories/post_repository.py` (`get_by_id`, `list_all`, `create`)
  * `app/repositories/target_repository.py` (`claim_due_targets`, `reset_for_retry`, `get_stats`)
  * `app/repositories/log_repository.py` (`get_by_target`, `list_recent`)
  * `app/repositories/account_repository.py` (`get_by_platform`, `save`)
  * `app/api/posts.py`, `app/api/targets.py`, `app/api/stats.py`, `app/api/logs.py` (updated to call repositories)
* **Risk Level**: **Low** (Refactoring queries; API contracts and behaviors remain identical)
* **How to Verify Nothing Broke**:
  ```powershell
  # 1. Run automated tests
  .\venv\Scripts\pytest tests/

  # 2. Test posts endpoint
  .\venv\Scripts\python -c "import httpx; assert httpx.get('http://127.0.0.1:8000/posts').status_code == 200"

  # 3. Test stats endpoint
  .\venv\Scripts\python -c "import httpx; assert httpx.get('http://127.0.0.1:8000/stats').status_code == 200"
  ```
* **Git Commit Message**:  
  `refactor: introduce repository layer and decouple queries from API routes`

---

### Stage 4: Service Layer Refactoring & Schemas Isolation
* **Goal**: Move all Pydantic request/response models into `app/schemas/`. Move scheduling, date conversion, and retry business logic from route handlers into domain services.
* **Files Created / Moved**:
  * `app/schemas/post_schemas.py` (`PostCreateRequest`, `PostResponse`, `TargetInput`)
  * `app/schemas/target_schemas.py` (`TargetResponse`, `LogResponse`)
  * `app/schemas/stats_schemas.py` (`StatsResponse`)
  * `app/services/scheduling_service.py` (date parsing, target creation, retry orchestration)
  * `app/api/posts.py`, `app/api/targets.py` (reduced to thin controllers)
* **Risk Level**: **Low** (Behavioral equivalence; pure structural cleanup)
* **How to Verify Nothing Broke**:
  ```powershell
  # 1. Create a test post via API
  .\venv\Scripts\python -c @"
  import httpx
  resp = httpx.post('http://127.0.0.1:8000/posts', json={
      'title': 'Refactor Verification',
      'body': 'Testing service layer extraction',
      'targets': [{'platform': 'discord', 'scheduled_at': '2026-10-30T18:00:00'}]
  })
  assert resp.status_code == 201, resp.text
  print('Post created successfully:', resp.json()['id'])
  "@

  # 2. Run pytest suite
  .\venv\Scripts\pytest tests/
  ```
* **Git Commit Message**:  
  `refactor: extract Pydantic schemas and scheduling business logic into services`

---

### Stage 5: Platform Plugin System & Dynamic Capabilities
* **Goal**: Reorganize platform publishers into self-contained plugins under `app/platforms/<name>/`. Introduce `PlatformCapabilities` and dynamic API endpoint `GET /api/platforms`.
* **Files Created / Moved**:
  * `app/platforms/base.py` (BasePlatformAdapter & PlatformCapabilities)
  * `app/platforms/registry.py` (Auto-discovery engine)
  * `app/platforms/telegram/` (adapter, capabilities, constants)
  * `app/platforms/discord/` (adapter, capabilities, constants)
  * `app/platforms/threads/` (adapter, capabilities, constants)
  * `app/api/platforms.py` (`GET /api/platforms` route)
  * Delete legacy `app/adapters/`
* **Risk Level**: **Medium** (Import paths change; registry dynamic loading)
* **How to Verify Nothing Broke**:
  ```powershell
  # 1. Verify dynamic platforms endpoint returns capabilities
  .\venv\Scripts\python -c "import httpx; data = httpx.get('http://127.0.0.1:8000/api/platforms').json(); assert 'threads' in data and 'telegram' in data; print(data.keys())"

  # 2. Run adapter test suite
  .\venv\Scripts\pytest tests/test_*adapter.py
  ```
* **Git Commit Message**:  
  `feat: introduce modular platform plugin architecture with dynamic capabilities`

---

### Stage 6: Multi-Account Architecture & Credential Vault
* **Goal**: Enable multiple accounts per platform by storing encrypted credentials in the database rather than static single values in `.env`.
* **Files Created / Moved**:
  * `alembic/versions/0002_multi_account_support.py` (adds `user_id`, `account_name`, `is_active` to accounts)
  * `app/services/account_vault_service.py` (encrypt, decrypt, test connection per account)
  * `app/api/accounts.py` (`GET /accounts`, `POST /accounts`, `DELETE /accounts/{id}`)
  * `app/templates/accounts.html` (UI to add Telegram bots, Discord webhooks, Threads logins)
* **Risk Level**: **Medium** (Modifies account linking for targets)
* **How to Verify Nothing Broke**:
  ```powershell
  # 1. Run Alembic migration
  .\venv\Scripts\alembic upgrade head

  # 2. Add a second Discord webhook via accounts API
  # 3. Create a post targeting both Discord accounts simultaneously
  # 4. Verify both dispatches succeed
  ```
* **Git Commit Message**:  
  `feat: implement multi-account management with encrypted credential storage`

---

### Stage 7: Worker Separation & Job Queue Abstraction
* **Goal**: Decouple the scheduler from the FastAPI web server. Create `app/worker.py` as a standalone daemon process with graceful shutdown and stuck-job recovery.
* **Files Created / Moved**:
  * `app/worker.py` (Standalone worker process entry point)
  * `app/services/job_queue.py` (JobQueue abstraction + DB-polling implementation)
  * `app/main.py` (Remove APScheduler startup from web lifespan)
  * `app/services/stuck_job_reaper.py` (Reclaims jobs orphaned in `processing` > 15m)
* **Risk Level**: **Medium** (Changes process execution model)
* **How to Verify Nothing Broke**:
  ```powershell
  # 1. Start web server in Terminal 1:
  .\venv\Scripts\uvicorn app.main:app --port 8000

  # 2. Start worker in Terminal 2:
  .\venv\Scripts\python -m app.worker

  # 3. Schedule a post for 'Post now' in Web UI and verify Terminal 2 dispatches it
  ```
* **Git Commit Message**:  
  `refactor: decouple background dispatcher into dedicated worker process`

---

### Stage 8: Media Pipeline & Storage Abstraction
* **Goal**: Add a `media` table and `StorageBackend` abstraction supporting local disk storage and S3/R2-compatible object storage.
* **Files Created / Moved**:
  * `alembic/versions/0003_media_pipeline.py` (adds `media` table)
  * `app/services/storage/base.py` (StorageBackend interface)
  * `app/services/storage/local.py` (Local disk storage)
  * `app/services/storage/s3.py` (S3/R2 storage backend)
  * `app/services/media_service.py` (validation, hash check, thumbnail/preview)
  * `app/api/media.py` (`POST /media/upload`, `GET /media/{id}`)
* **Risk Level**: **Low** (New additive feature)
* **How to Verify Nothing Broke**:
  ```powershell
  # 1. Run migration
  .\venv\Scripts\alembic upgrade head

  # 2. Upload an image via API and verify file on disk + public URL returned
  ```
* **Git Commit Message**:  
  `feat: add media library pipeline with local and S3 storage backends`

---

### Stage 9: Observability, Structured Logging & Health Probes
* **Goal**: Implement JSON structured logging, global secret masking filter, request IDs (`X-Request-ID`), and `/ready` probes.
* **Files Created / Moved**:
  * `app/core/logging.py` (JSON formatter & secret-masking filter)
  * `app/middleware/request_id.py` (Request ID correlation middleware)
  * `app/api/health.py` (`/health` liveness + `/ready` database/worker probe)
* **Risk Level**: **Low** (Observability enhancement)
* **How to Verify Nothing Broke**:
  ```powershell
  # 1. Verify health and readiness endpoints
  .\venv\Scripts\python -c "import httpx; assert httpx.get('http://127.0.0.1:8000/health').status_code == 200; assert httpx.get('http://127.0.0.1:8000/ready').status_code == 200"

  # 2. Verify X-Request-ID header exists in responses
  .\venv\Scripts\python -c "import httpx; resp = httpx.get('http://127.0.0.1:8000/health'); assert 'X-Request-ID' in resp.headers; print('Request-ID:', resp.headers['X-Request-ID'])"
  ```
* **Git Commit Message**:  
  `feat: add structured JSON logging, secret redaction, and readiness probes`

---

### Stage 10: Jinja2 Templates & CSS Modularization
* **Goal**: Modularize `styles.css` into distinct token and component stylesheets. Extract reusable Jinja2 components (`platform_chip.html`, `badge.html`, `timeline.html`). Drive the composer platform chips dynamically from the backend capabilities API.
* **Files Created / Moved**:
  * `app/static/css/tokens.css`, `app/static/css/layout.css`, `app/static/css/components.css`
  * `app/templates/components/platform_chip.html`, `app/templates/components/badge.html`
  * `app/static/js/composer.js` (modularized from `app.js`)
* **Risk Level**: **Low** (Frontend template refactoring)
* **How to Verify Nothing Broke**:
  ```powershell
  # 1. Visit Dashboard, Composer, Calendar, and Detail pages in browser
  # 2. Verify light/dark theme toggle operates smoothly
  # 3. Schedule a multi-part Threads post and verify live preview renders properly
  ```
* **Git Commit Message**:  
  `refactor: modularize Jinja2 templates, CSS design tokens, and composer JS`

---

### Stage 11: Developer Tooling, Docker & CI/CD
* **Goal**: Add a `Makefile`, code quality configuration (`ruff.toml`, `mypy.ini`), a multi-stage `Dockerfile`, `docker-compose.yml`, and a GitHub Actions workflow.
* **Files Created / Moved**:
  * `Makefile` (Developer command runner)
  * `ruff.toml` (Fast linter and formatter config)
  * `mypy.ini` (Type checking config)
  * `Dockerfile` (Multi-stage Python container build)
  * `docker-compose.yml` (Web + Worker + PostgreSQL + Redis)
  * `.github/workflows/ci.yml` (Automated CI test & lint pipeline)
* **Risk Level**: **Low** (Tooling & containerization)
* **How to Verify Nothing Broke**:
  ```powershell
  # 1. Run lint check
  .\venv\Scripts\ruff check app/ tests/

  # 2. Run type checker
  .\venv\Scripts\mypy app/

  # 3. Build and run containers (if Docker installed)
  docker compose up --build -d
  ```
* **Git Commit Message**:  
  `ci: add docker compose, ruff, mypy, and github actions pipeline`

---

## 3. Decisions I Need From You

Before starting implementation of these stages, please confirm your preferences on the following key design questions:

| # | Decision Topic | Option A (Simpler / Local-First) | Option B (Cloud / Scalable) | Why It Matters |
| :- | :--- | :--- | :--- | :--- |
| **1** | **User Tenancy** | **Single-User**: Tool remains personal; no login screens. Hardcoded default `user_id=1`. | **Multi-User**: Email/password authentication, JWT sessions, multi-user isolation. | Determines whether we build a login/signup screen in Stage 6. |
| **2** | **Next Platforms** | **Bluesky & Reddit** | **Instagram & LinkedIn** | Influences whether we build media upload pipelines first (Instagram requires public media URLs). |
| **3** | **Hosting Target** | **Single VPS (Hetzner / DigitalOcean)** with Docker Compose | **Serverless / PaaS (Railway / Render / Fly.io)** | Dictates whether we use local disk volume storage or S3/Cloudflare R2 for media files. |
| **4** | **Queue Engine** | **Database-Polling Worker (SQLite/Postgres)** (Zero extra infrastructure) | **Redis + ARQ / Celery** | Determines whether Redis is required in your local dev setup. |
| **5** | **Frontend Path** | **Stick with Jinja2**: Keep server-rendered templates, just clean them up. | **Separate SPA (React / Vue / Svelte)** | Determines whether we invest effort in modularizing Jinja2 or move directly to a Vite-based frontend. |

