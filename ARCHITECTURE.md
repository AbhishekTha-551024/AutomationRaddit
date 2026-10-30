# Target Architecture: Social Media Post Scheduler

**Document Version**: 1.0  
**Status**: Target Design Specification  
**Author**: Senior Backend Architect  

---

## 1. System Overview & Layering

The application will follow a strict **Layered Clean Architecture**. High-level modules do not depend on low-level modules; both depend on abstractions. HTTP routes are completely decoupled from database queries and scheduling logic.

```
+-----------------------------------------------------------------------------------+
|                                 CLIENT CLIENTS                                    |
|   Web Browser (Jinja2 / Future SPA)         API Clients (cURL, Webhooks, Scripts) |
+-----------------------------------------------------------------------------------+
                                         |
                                         v
+-----------------------------------------------------------------------------------+
|                             1. API PRESENTATION LAYER                             |
|   app/api/v1/ (Thin JSON routes)       app/web/ (HTML Controllers & ViewModels)   |
|   - Request validation (Pydantic DTOs) - Response status codes & serialization    |
+-----------------------------------------------------------------------------------+
                                         |
                                         v
+-----------------------------------------------------------------------------------+
|                                2. SERVICES LAYER                                  |
|   app/services/                                                                   |
|   - PostSchedulingService    - DispatchService         - AccountVaultService     |
|   - MediaProcessingService   - TokenLifecycleService   - RetryPolicyService      |
+-----------------------------------------------------------------------------------+
                       |                                     |
                       v                                     v
+----------------------------------+     +------------------------------------------+
|      3. REPOSITORIES LAYER       |     |        4. PLATFORMS / ADAPTERS           |
|   app/repositories/              |     |   app/platforms/<name>/                  |
|   - PostRepository               |     |   - Telegram, Discord, Threads           |
|   - TargetRepository             |     |   - Reddit, Instagram, Bluesky, LinkedIn |
|   - AccountRepository            |     |   - Capabilities & Validation Rules      |
|   - MediaRepository              |     |   - Token refresh & error translation    |
+----------------------------------+     +------------------------------------------+
                       |                                     |
                       v                                     v
+-----------------------------------------------------------------------------------+
|                             5. PERSISTENCE & STORAGE                              |
|   PostgreSQL / SQLite Database          Local Disk / S3 / Cloudflare R2 Storage   |
+-----------------------------------------------------------------------------------+
```

### Layer Responsibilities & Rules
1. **API Layer (`app/api/`, `app/web/`)**:
   - **Rule**: Absolutely NO SQL queries, no direct database commits, and no adapter calls.
   - Accepts incoming requests, validates input using Pydantic DTO schemas, invokes service methods, and returns formatted responses.
2. **Services Layer (`app/services/`)**:
   - **Rule**: Contains all business decisions, workflow logic, transaction boundaries, and orchestration.
   - Example: Scheduling a post, deciding retry backoff delays, verifying if a post adheres to platform limits, claiming due items.
3. **Repositories Layer (`app/repositories/`)**:
   - **Rule**: The only layer containing raw SQLAlchemy queries (`select`, `insert`, `update`, `delete`).
   - Returns model instances or domain objects. Provides reusable queries like `get_due_targets_for_claim()`, `find_by_id()`, `save()`.
4. **Models Layer (`app/models/`)**:
   - Pure SQLAlchemy 2.0 declarative database models defining tables, relationships, foreign keys, and column constraints.
5. **Schemas Layer (`app/schemas/`)**:
   - Pydantic DTO models for data transfer: Request inputs, Response models, and internal Transfer objects.

---

## 2. Platform Plugin System

Every social network lives in an isolated, self-contained plugin directory under `app/platforms/<name>/`. Adding a new platform requires creating one folder and zero modifications to existing core code.

### 2.1 Platform Folder Structure
```
app/platforms/
├── __init__.py
├── base.py                   # BasePlatformAdapter & PlatformCapabilities definitions
├── registry.py               # Auto-discovery loader
├── discord/
│   ├── __init__.py           # Registers the platform plugin
│   ├── adapter.py            # Implements BasePlatformAdapter.publish()
│   ├── capabilities.py       # Capabilities definition (limits, media, auth)
│   └── constants.py          # Discord API URLs, rate limits, error codes
├── telegram/
│   ├── ...
├── threads/
│   ├── ...
└── (future: bluesky, reddit, instagram, linkedin, x, youtube)
```

### 2.2 Platform Capabilities Contract
Each platform declares its features using a strongly typed dataclass:

```python
# app/platforms/base.py
from dataclasses import dataclass, field
from typing import List, Optional, Set
from enum import Enum

class AuthType(str, Enum):
    WEBHOOK = "webhook"       # Discord Incoming Webhook
    BOT_TOKEN = "bot_token"   # Telegram Bot Token
    OAUTH2 = "oauth2"         # Meta Threads, Reddit, Instagram, LinkedIn

class MediaType(str, Enum):
    IMAGE = "image"
    VIDEO = "video"
    GIF = "gif"

@dataclass
class PlatformCapabilities:
    platform_name: str
    display_name: str
    icon_svg: str
    auth_type: AuthType
    max_text_length: int
    supports_thread_chains: bool = False
    thread_chain_delimiter: Optional[str] = None
    thread_part_max_length: Optional[int] = None
    supports_native_scheduling: bool = False
    supported_media_types: Set[MediaType] = field(default_factory=set)
    max_media_count: int = 0
    max_media_size_bytes: int = 0
    requires_public_media_url: bool = False
    rate_limit_rpm: int = 30
```

### 2.3 Auto-Discovery Registry
`app/platforms/registry.py` inspects subdirectories under `app/platforms/` on startup:
1. It imports each platform module and registers its `adapter` and `capabilities`.
2. Exposes `GET /api/platforms`: returns capability metadata to the frontend.
3. **Frontend Dynamic UI**: The Composer in `app.js` and `composer.html` queries this endpoint. Platform chips, character counters, media upload dropzones, and thread-chain hints are generated dynamically from capabilities.

---

## 3. Multi-Account Support

Currently, credentials for Telegram and Discord live in `.env` as single static variables. In the target architecture, all credentials live in the encrypted `accounts` database table, allowing multiple accounts per platform (e.g. 3 Discord channels, 2 Telegram groups, 2 Threads profiles).

### 3.1 Data Model
```
+----------------------------------------------------+
|                      accounts                      |
+----------------------------------------------------+
| id: Integer (PK)                                   |
| user_id: Integer (FK -> users.id, nullable for now)|
| platform: String(50) (e.g. 'discord', 'telegram')  |
| account_name: String(100) (e.g. 'Dev Announcements')|
| encrypted_credentials: Text (Fernet JSON string)   |
| token_expiry: DateTime (UTC, optional)             |
| is_active: Boolean (default True)                  |
| created_at: DateTime (UTC)                         |
+----------------------------------------------------+
                          | 1
                          |
                          | N
+----------------------------------------------------+
|                    post_targets                    |
+----------------------------------------------------+
| id: Integer (PK)                                   |
| post_id: Integer (FK -> posts.id)                  |
| platform: String(50)                               |
| account_id: Integer (FK -> accounts.id)            |
| scheduled_at: DateTime (UTC)                       |
| status: Enum (pending, processing, posted, failed) |
| ...                                                |
+----------------------------------------------------+
```

### 3.2 Account Credential Vault (`AccountVaultService`)
- All credentials (tokens, webhooks, refresh tokens, bot secrets) are stored as JSON strings encrypted via AES-128-CBC (`Fernet`).
- The encryption key is sourced strictly from `ENCRYPTION_KEY` in the host environment.
- Credentials are decrypted in-memory only at the exact moment of HTTP dispatch.
- A single account management screen in the web UI will allow users to add, rename, and test accounts.

---

## 4. Media Pipeline

Platforms like Threads and Instagram do not accept direct binary file uploads for text/media posts; they require **public URLs** that Meta's servers fetch. Other platforms (Telegram, Discord) accept direct multi-part file uploads.

### 4.1 Storage Abstraction
```python
# app/services/storage/base.py
from abc import ABC, abstractmethod
from typing import BinaryIO

class StorageBackend(ABC):
    @abstractmethod
    def save(self, file_obj: BinaryIO, filename: str) -> str:
        """Saves file and returns storage identifier."""
        pass

    @abstractmethod
    def get_public_url(self, file_id: str) -> str:
        """Returns a publicly accessible HTTP URL for platform fetch."""
        pass

    @abstractmethod
    def delete(self, file_id: str) -> None:
        """Deletes file from storage."""
        pass
```

### 4.2 Storage Implementations
1. **Local Disk Storage (`LocalStorageBackend`)**:
   - Files saved to `app/uploads/<year>/<month>/<hash>.<ext>`.
   - Served via FastAPI static mount `/media/...`.
   - During local dev, tools like `ngrok` or Cloudflare Tunnels provide the public HTTPS domain.
2. **S3 / Cloudflare R2 Storage (`S3StorageBackend`)**:
   - Zero-code change swap via configuration (`STORAGE_BACKEND=s3`).
   - Generates signed or public bucket URLs for Cloudflare R2 or Amazon S3.

### 4.3 Media Validation & Cleanup
- `MediaProcessingService` validates file type, file size, image resolution, and aspect ratio against the destination platform's `PlatformCapabilities`.
- Nightly cleanup sweep removes orphan media files not attached to any post after 48 hours.

---

## 5. Job Execution & Worker Separation

Running background jobs inside the web server process causes locking issues on SQLite, blocks under multi-worker setups, and crashes the scheduler whenever Uvicorn reloads.

### 5.1 Architecture: Web vs Worker Separation
```
+---------------------------------------------------+
|                WEB SERVER PROCESS                 |
|             (uvicorn app.main:app)                |
|  - Handles HTTP API & Jinja Views                 |
|  - Creates posts and inserts 'pending' targets    |
+---------------------------------------------------+
                         |
                         | (Database or Redis)
                         v
+---------------------------------------------------+
|               WORKER PROCESS RUNNER               |
|            (python -m app.worker)                 |
|  - Runs scheduler loop / job consumer             |
|  - Claims pending targets atomically              |
|  - Dispatches to platform adapters                |
|  - Manages retries, backoff, and state            |
+---------------------------------------------------+
```

### 5.2 Job Queue Abstraction
```python
class JobQueue(ABC):
    @abstractmethod
    def enqueue_target(self, target_id: int, run_at: datetime) -> str:
        pass

    @abstractmethod
    def claim_due_targets(self, limit: int = 10) -> List[PostTarget]:
        pass
```
- **Phase 1 (Immediate)**: Enhanced DB-polling runner running in a standalone process (`app/worker.py`) using row-level locking.
- **Phase 2 (Scalable)**: Redis-backed queue using **ARQ** (async, fast, lightweight Python queue) or **Celery**.

### 5.3 Concurrency & Lock Safety
- **PostgreSQL**: `SELECT * FROM post_targets WHERE status='pending' AND scheduled_at <= NOW() FOR UPDATE SKIP LOCKED LIMIT 10;`. Multiple worker processes can run concurrently without race conditions.
- **SQLite**: Keep the atomic update claim `UPDATE post_targets SET status='processing' WHERE id=:id AND status='pending'`.
- **Stuck Job Recovery**: If a worker crashes while processing, a target can stay in `processing`. The worker runs a reaper every 10 minutes that checks for targets stuck in `processing` with no updates for $> 15$ minutes, resetting them to `pending` with a warning log.
- **Graceful Shutdown**: The worker listens for `SIGTERM` and `SIGINT`, waits up to 30 seconds for active network requests to complete, commits `publish_state`, and exits cleanly.

---

## 6. Database & Migration Strategy

### 6.1 Database Schema Improvements (PostgreSQL & SQLite)
- **Alembic Migrations**: All schema modifications are tracked as versioned migration scripts in `alembic/versions/`. Replaces `Base.metadata.create_all()` and manual `ALTER TABLE` statements.
- **Indexes**:
  - `idx_targets_due`: Composite index on `post_targets (status, scheduled_at, next_retry_at)`.
  - `idx_targets_post_id`: Foreign key index on `post_targets (post_id)`.
  - `idx_logs_target_id`: Foreign key index on `logs (post_target_id, created_at)`.
- **Soft Deletes**: Posts and Accounts will have a `deleted_at: Optional[datetime]` column to preserve historical logs and analytics even when a user deletes a post.
- **UTC Enforcement**: All timestamps stored with timezone (`TIMESTAMP WITH TIME ZONE` in Postgres; UTC ISO strings in SQLite).

### 6.2 SQLite to PostgreSQL Migration Plan
1. Baseline current SQLite schema with an Alembic migration (`0001_initial_baseline.py`).
2. Add a database dump/seed utility script (`scripts/migrate_sqlite_to_postgres.py`) using SQLAlchemy reflection to stream all existing posts, targets, accounts, and logs from `scheduler.db` into PostgreSQL without data loss.

---

## 7. Configuration Management

### 7.1 Layered Settings Architecture
```python
class Settings(BaseSettings):
    # App
    APP_ENV: str = "development"
    APP_SECRET_KEY: str
    ENCRYPTION_KEY: str
    
    # Database
    DATABASE_URL: str = "sqlite:///./scheduler.db"
    DB_POOL_SIZE: int = 5
    
    # Scheduler
    WORKER_CONCURRENCY: int = 4
    DEFAULT_TIMEZONE: str = "Asia/Kolkata"
    RETRY_DELAYS_MINUTES: str = "5,15,60"
    
    # Storage
    STORAGE_BACKEND: str = "local" # 'local' or 's3'
    MEDIA_UPLOAD_DIR: str = "app/uploads"
    
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
```
- Startup validator raises fatal, easy-to-read errors if required secrets (`ENCRYPTION_KEY`, `DATABASE_URL`) are missing or misconfigured.
- Clean `.env.example` file that is always kept in sync with code additions.

---

## 8. Auth & Multi-User Readiness

The architecture supports a smooth transition to multi-tenancy without modifying post or target structures later.

### 8.1 Schema Design (Ready for Multi-User)
```sql
CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    hashed_password VARCHAR(255) NOT NULL,
    full_name VARCHAR(100),
    role VARCHAR(20) DEFAULT 'member', -- 'admin', 'member'
    timezone VARCHAR(50) DEFAULT 'Asia/Kolkata',
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Posts and Accounts belong to a user
ALTER TABLE posts ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE accounts ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE;
```

### 8.2 Authentication Strategy
- **Single-User Mode (Current)**: A dummy default user (`user_id=1`) is automatically resolved by the dependency `get_current_user`.
- **Multi-User Mode (Future)**: `get_current_user` extracts JWT cookies or API bearer tokens (`Authorization: Bearer <token>`). Every repository query automatically filters by `WHERE user_id = current_user.id`.

---

## 9. Observability & Diagnostics

1. **Structured Logging**:
   - Replace standard string logs with structured JSON logging (`structlog` or standard library `json` logging).
   - Every HTTP request generates a `request_id` (UUIDv4) passed via the `X-Request-ID` header.
   - When a job is claimed, `target_id`, `post_id`, and `platform` are injected into the logger context.
2. **Secret Redaction Filter**:
   - A global logging filter intercepts all output lines, checking against loaded platform secrets and tokens, replacing them with `[REDACTED]`.
3. **Health & Readiness Probes**:
   - `GET /health`: Liveness probe (returns 200 if the web process is alive).
   - `GET /ready`: Readiness probe (verifies database connectivity, storage writeability, and scheduler worker heartbeat).

---

## 10. Advanced Scalability Features

1. **Recurring Post Schedules**:
   - Add `is_recurring: bool` and `cron_expression: Optional[str]` to posts. When a post publishes, the service automatically clones and schedules the next recurrence.
2. **Bulk CSV Import**:
   - Simple CSV upload supporting columns: `title, body, scheduled_at_ist, platforms`.
3. **Drafts & Approval Workflow**:
   - `status: Enum('draft', 'scheduled', 'published')`. Posts in draft mode are excluded from the dispatcher.
4. **Platform Failure Webhooks**:
   - An alert dispatcher service that sends an emergency notification to a private Telegram or Discord channel if a scheduled target permanently fails after max retries.
5. **Rate-Limit Token Buckets**:
   - In-memory / Redis token bucket tracking published posts per platform to avoid hitting Meta or Twitter API rate limits.

---

## 11. Testing & Quality Assurance Strategy

1. **Pytest Suite (`pytest.ini`)**:
   - Migrate from standard library `unittest` to `pytest` with fixtures for database sessions, fake clocks (`freezegun`), and mock HTTP clients.
2. **Fake Platform Adapter (`FakePlatformAdapter`)**:
   - A mock adapter that can be programmed in tests to simulate:
     - Instant success
     - 429 Rate limiting
     - Network timeouts
     - Permanent client errors
     - Multi-part thread chains
3. **Transactional Test Database**:
   - Each test runs inside a nested transaction that rolls back immediately upon completion, guaranteeing fast and completely isolated tests.
4. **CI/CD Pipeline (GitHub Actions)**:
   - On every pull request and push: runs `ruff check`, `ruff format --check`, `mypy app/`, and `pytest --cov=app`.

---

## 12. Developer Experience & Containerization

### 12.1 Tooling
- **`Makefile`**: Short commands for developers:
  - `make run-web`: Starts web API.
  - `make run-worker`: Starts scheduler background worker.
  - `make test`: Runs full test suite with coverage.
  - `make lint`: Runs Ruff linter and formatter.
  - `make migrate`: Runs Alembic database migrations.
- **Type Safety**: `mypy` strict type checking on models, services, and repositories.

### 12.2 Production Docker Setup
```yaml
# docker-compose.yml
services:
  web:
    build: .
    command: uvicorn app.main:app --host 0.0.0.0 --port 8000
    ports:
      - "8000:8000"
    env_file: .env
    depends_on:
      - db
      - redis

  worker:
    build: .
    command: python -m app.worker
    env_file: .env
    depends_on:
      - db
      - redis

  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: scheduler
      POSTGRES_USER: scheduler_user
      POSTGRES_PASSWORD: secret_password
    volumes:
      - postgres_data:/var/lib/postgresql/data

  redis:
    image: redis:7-alpine
    volumes:
      - redis_data:/data

volumes:
  postgres_data:
  redis_data:
```

---

## 13. Frontend Architecture

### 13.1 Jinja2 Modularization
- Split `styles.css` into modular CSS files:
  - `base.css`: CSS reset, typography, light/dark color variables.
  - `layout.css`: Sidebar, app shell, header, drawer.
  - `components.css`: Buttons, chips, badges, toast, modal.
  - `views/`: `dashboard.css`, `composer.css`, `calendar.css`.
- Modularize Jinja templates into reusable components:
  - `components/platform_chip.html`: Reusable platform selector.
  - `components/badge.html`: Reusable status badges.
  - `components/timeline.html`: Audit logs viewer.

### 13.2 Separation Readiness
- Because all API endpoints are standard RESTful JSON (`/api/v1/posts`, `/api/v1/targets`, `/api/v1/platforms`), the frontend can be swapped for a modern Single Page Application (React / Vue / Svelte / Next.js) in the future without changing a single line of backend logic.

