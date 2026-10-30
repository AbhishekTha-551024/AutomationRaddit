# Implementation Plan: Social Media Post Scheduler

**System Architecture & Implementation Plan**  
**Author:** Senior Backend Engineering Team  
**Status:** DRAFT (Awaiting User Review)  
**Target Environment:** Windows 10/11, Python 3.10+, Local execution  

---

## 1. Executive Summary & Core Requirements

This document specifies the technical design, database schema, component boundaries, execution flow, and step-by-step implementation phases for a local-first **Social Media Post Scheduler** built in Python.

### Core Goals:
1. **Multi-Platform Scheduling:** Compose a single post (text content, optional image/video) and schedule it for specific broadcast timestamps across multiple social platforms (**Telegram**, **Reddit**, **Instagram**).
2. **Reliable Automated Dispatch:** APScheduler running in-process polls the database every 60 seconds, claims pending posts using atomic transactions, and delegates publication to platform-specific adapters.
3. **Resilience & Fault Tolerance:** Automatic retry logic with exponential backoff (5 min, 15 min, 60 min; terminal failure after 3 attempts). Crash recovery on application startup for jobs interrupted mid-flight.
4. **Auditability & Observability:** Granular log records per target post attempt, structured execution traces, and status inspection endpoints.
5. **Clean Architecture & Extensibility:** Strict Adapter Pattern (`publish(post, target) -> PublishResult`). Adding support for any new platform requires adding exactly one adapter file without touching business logic or scheduling internals.
6. **Timezone Accuracy:** All timestamps are strictly stored in **UTC** in the database and converted to **IST (Asia/Kolkata)** exclusively at the presentation/display layer.

---

## 2. Directory & Package Structure

```text
social-scheduler/
├── .env.example              # Template for environment variables and secrets
├── .env                      # Local secret configuration (git-ignored)
├── .gitignore                # Git ignore rules for Python, SQLite, media, env
├── requirements.txt          # Pinned production and development dependencies
├── README.md                 # Project setup and usage instructions
├── PLAN.md                   # This implementation plan
├── main.py                   # FastAPI application factory and ASGI entrypoint
├── app/
│   ├── __init__.py
│   ├── config.py             # Pydantic BaseSettings, env parsing, timezone setup
│   ├── database.py           # SQLAlchemy 2.0 engine, SessionLocal, Base model
│   ├── api/                  # FastAPI routers (thin HTTP controllers)
│   │   ├── __init__.py
│   │   ├── deps.py           # Dependency injection (DB session, auth, services)
│   │   ├── posts.py          # Post CRUD, target attachments, and manual triggers
│   │   ├── targets.py        # PostTarget status query, manual retry triggers
│   │   ├── accounts.py       # Account credential registration and inspection
│   │   ├── logs.py           # Target audit logs and execution traces
│   │   └── dashboard.py      # Jinja2 HTML views (Phase 6)
│   ├── models/               # SQLAlchemy 2.0 Declarative Models
│   │   ├── __init__.py
│   │   ├── post.py           # Post entity
│   │   ├── target.py         # PostTarget entity (platform schedule & state)
│   │   ├── account.py        # Account credentials entity (encrypted)
│   │   └── log.py            # TargetLog execution history entity
│   ├── schemas/              # Pydantic v2 validation models & DTOs
│   │   ├── __init__.py
│   │   ├── post.py           # PostCreate, PostUpdate, PostResponse
│   │   ├── target.py         # TargetCreate, TargetResponse, TargetStatusUpdate
│   │   ├── account.py        # AccountCreate, AccountResponse (redacted secrets)
│   │   └── log.py            # LogEntryResponse
│   ├── services/             # Core business logic layer
│   │   ├── __init__.py
│   │   ├── post_service.py   # Post & target orchestration and validation
│   │   ├── crypto_service.py # AES/Fernet encryption for stored credentials
│   │   └── log_service.py    # Database logging handler for scheduler runs
│   ├── adapters/             # Platform publisher adapters (Adapter Pattern)
│   │   ├── __init__.py
│   │   ├── base.py           # Abstract Base Adapter & PublishResult definition
│   │   ├── registry.py       # Adapter registry & dynamic factory lookup
│   │   ├── telegram.py       # Telegram Bot API implementation (Phase 2)
│   │   ├── reddit.py         # PRAW Reddit implementation (Phase 5)
│   │   └── instagram.py      # Instagram Graph API implementation (Phase 7)
│   ├── scheduler/            # Background scheduling engine
│   │   ├── __init__.py
│   │   ├── engine.py         # APScheduler lifecycle and runner configuration
│   │   ├── worker.py         # Queue poller, atomic lock claims, and dispatch
│   │   └── recovery.py       # Startup crash recovery for orphaned 'processing' tasks
│   ├── templates/            # Jinja2 HTML templates for Web Dashboard (Phase 6)
│   │   ├── base.html
│   │   ├── dashboard.html
│   │   └── create_post.html
│   └── static/               # Static assets (CSS/JS) for dashboard
│       ├── css/
│       └── js/
├── media/                    # Local storage for post media attachments (images/videos)
└── tests/                    # Pytest automated test suite
    ├── __init__.py
    ├── conftest.py           # Test database fixtures, client overrides, mock adapters
    ├── unit/                 # Unit tests for crypto, adapters, state transitions
    │   ├── test_crypto.py
    │   ├── test_telegram_adapter.py
    │   └── test_state_machine.py
    └── integration/          # API endpoint tests and scheduler flow tests
        ├── test_api_posts.py
        └── test_scheduler_worker.py
```

### Folder Responsibilities (One-Line Descriptions):
- **`app/api`**: Exposes thin HTTP route handlers delegating requests to services.
- **`app/models`**: Defines SQLAlchemy ORM entities and database table relationships.
- **`app/schemas`**: Validates request payloads and formats outgoing JSON responses with Pydantic.
- **`app/services`**: Encapsulates business logic, credential encryption, and post workflows.
- **`app/adapters`**: Implements platform-specific publishing logic behind a unified interface.
- **`app/scheduler`**: Manages APScheduler background polling, atomic job claims, and retries.
- **`app/templates` & `app/static`**: Houses Jinja2 UI views and styling for the local web dashboard.
- **`tests`**: Contains automated unit and integration tests verifying API, state machine, and adapters.

---

## 3. Database Schema & Models (SQLAlchemy 2.x)

The database layer utilizes **SQLAlchemy 2.0 DeclarativeBase** with typed mapping (`Mapped`, `mapped_column`). Designed natively for SQLite with portable types (`DateTime(timezone=True)`, `String`, `Text`, `Integer`) enabling zero-friction migration to PostgreSQL via changing `DATABASE_URL`.

```mermaid
erDiagram
    POSTS ||--o{ POST_TARGETS : "has one or more"
    ACCOUNTS ||--o{ POST_TARGETS : "provides credentials for"
    POST_TARGETS ||--o{ LOGS : "generates"

    POSTS {
        int id PK
        string title "nullable"
        text body "required"
        string media_path "nullable"
        datetime created_at "UTC"
    }

    ACCOUNTS {
        int id PK
        string platform "telegram|reddit|instagram"
        string name "descriptive label"
        text encrypted_credentials "AES/Fernet cipher"
        datetime token_expiry "UTC, nullable"
    }

    POST_TARGETS {
        int id PK
        int post_id FK
        string platform "telegram|reddit|instagram"
        int account_id FK "nullable"
        datetime scheduled_at "UTC"
        string status "pending|processing|posted|failed"
        int attempts "default 0"
        datetime next_retry_at "UTC, nullable"
        string external_post_id "nullable"
        text error_message "nullable"
    }

    LOGS {
        int id PK
        int post_target_id FK
        string level "INFO|WARNING|ERROR|DEBUG"
        text message
        datetime created_at "UTC"
    }
```

### Detailed Table Specifications

#### 1. `posts`
Represents the core creative content to be published.
- `id`: `INTEGER`, Primary Key, Autoincrement.
- `title`: `VARCHAR(255)`, Nullable. (Used as Reddit title, Instagram caption header, or Telegram message header).
- `body`: `TEXT`, Not Null. (The post body or caption).
- `media_path`: `VARCHAR(500)`, Nullable. (Local filesystem path or relative path inside `./media`).
- `created_at`: `DATETIME(timezone=True)`, Not Null, Default `func.now()` (UTC).
- **Relationships:**
  - `targets`: One-to-Many with `post_targets`, `cascade="all, delete-orphan"`.

#### 2. `accounts`
Holds platform authentication details and API tokens in encrypted format.
- `id`: `INTEGER`, Primary Key, Autoincrement.
- `platform`: `VARCHAR(50)`, Not Null, Index. (`"telegram"`, `"reddit"`, `"instagram"`).
- `name`: `VARCHAR(100)`, Not Null. (Human-readable identifier, e.g., `"TechChannel_Bot"`, `"Reddit_Personal"`).
- `encrypted_credentials`: `TEXT`, Not Null. (Fernet-encrypted JSON payload containing API tokens, client secrets, chat IDs).
- `token_expiry`: `DATETIME(timezone=True)`, Nullable (UTC). (Used for OAuth refresh tokens, e.g., Instagram long-lived tokens).
- **Relationships:**
  - `post_targets`: One-to-Many with `post_targets`.

#### 3. `post_targets`
Represents a scheduled dispatch of a post to a specific platform account.
- `id`: `INTEGER`, Primary Key, Autoincrement.
- `post_id`: `INTEGER`, Foreign Key referencing `posts.id` (`ondelete="CASCADE"`), Not Null, Index.
- `platform`: `VARCHAR(50)`, Not Null, Index.
- `account_id`: `INTEGER`, Foreign Key referencing `accounts.id` (`ondelete="SET NULL"`), Nullable, Index.
- `scheduled_at`: `DATETIME(timezone=True)`, Not Null, Index. (Intended publication time in UTC).
- `status`: `VARCHAR(20)`, Not Null, Default `"pending"`, Index. (Allowed values: `"pending"`, `"processing"`, `"posted"`, `"failed"`).
- `attempts`: `INTEGER`, Not Null, Default `0`. (Count of publish attempts made).
- `next_retry_at`: `DATETIME(timezone=True)`, Nullable, Index. (Next earliest dispatch time if retrying).
- `external_post_id`: `VARCHAR(255)`, Nullable. (ID/URL assigned by platform upon successful publishing).
- `error_message`: `TEXT`, Nullable. (Last encountered error traceback or summary).
- **Relationships:**
  - `post`: Many-to-One with `posts`.
  - `account`: Many-to-One with `accounts`.
  - `logs`: One-to-Many with `logs`, `cascade="all, delete-orphan"`.

#### 4. `logs`
Immutable execution and audit logs tied directly to specific target attempts.
- `id`: `INTEGER`, Primary Key, Autoincrement.
- `post_target_id`: `INTEGER`, Foreign Key referencing `post_targets.id` (`ondelete="CASCADE"`), Not Null, Index.
- `level`: `VARCHAR(20)`, Not Null. (`"INFO"`, `"WARNING"`, `"ERROR"`, `"DEBUG"`).
- `message`: `TEXT`, Not Null. (Descriptive log entry, redacted of secrets).
- `created_at`: `DATETIME(timezone=True)`, Not Null, Default `func.now()` (UTC).
- **Relationships:**
  - `post_target`: Many-to-One with `post_targets`.

---

## 4. API Endpoints Specification

All API paths are prefixed with `/api`. Payloads are strictly validated with Pydantic v2 schemas. Sensitive tokens are never returned in responses.

| Method | Endpoint | Purpose | Request Body / Params | Response Shape |
|---|---|---|---|---|
| `GET` | `/health` | System health check & scheduler status | None | `{"status": "ok", "utc_time": "...", "ist_time": "...", "scheduler_running": true}` |
| `POST` | `/api/posts` | Create new post and schedule targets | `PostCreateRequest` (title, body, media_path, targets: list of {platform, account_id, scheduled_at_ist}) | `PostDetailResponse` (id, title, body, media_path, created_at_ist, targets: [...]) |
| `GET` | `/api/posts` | List posts with pagination | Query params: `page=1`, `limit=20`, `platform=...` | `PostListResponse` (total, items: List[PostSummaryResponse]) |
| `GET` | `/api/posts/{id}` | Retrieve post details, targets & status | Path param: `id` | `PostDetailResponse` (with full targets and recent logs) |
| `DELETE` | `/api/posts/{id}` | Delete post and cancel pending targets | Path param: `id` | `{"message": "Post and associated pending targets cancelled."}` |
| `POST` | `/api/posts/{id}/publish-now` | Force immediate publish trigger | Path param: `id` | `{"message": "Targets marked for immediate execution."}` |
| `GET` | `/api/targets` | List targets filtered by status/date | Query params: `status`, `platform`, `date` | `TargetListResponse` (items: List[TargetResponse]) |
| `GET` | `/api/targets/{id}` | Get target status and execution logs | Path param: `id` | `TargetDetailResponse` (status, attempts, next_retry_at, logs: [...]) |
| `POST` | `/api/targets/{id}/retry` | Manually reset a failed target to pending | Path param: `id` | `TargetResponse` (status reset to "pending", attempts preserved or reset) |
| `POST` | `/api/accounts` | Register platform credentials (encrypted) | `AccountCreateRequest` (platform, name, credentials: dict) | `AccountResponse` (id, platform, name, token_expiry, credentials_configured: true) |
| `GET` | `/api/accounts` | List configured accounts (no secrets) | Query param: `platform` (optional) | `List[AccountResponse]` |
| `GET` | `/api/logs` | Query system logs | Query params: `target_id`, `level`, `limit` | `List[LogEntryResponse]` |
| `GET` | `/dashboard` | HTML Web Dashboard (Phase 6) | None | HTML page rendered via Jinja2 |

### Request & Response Schema Shapes (Pydantic v2)

```python
# app/schemas/post.py
class TargetScheduleInput(BaseModel):
    platform: Literal["telegram", "reddit", "instagram"]
    account_id: Optional[int] = None
    scheduled_at: datetime  # User submits ISO string (can be IST or UTC with offset)

class PostCreateRequest(BaseModel):
    title: Optional[str] = Field(None, max_length=255)
    body: str = Field(..., min_length=1)
    media_path: Optional[str] = None
    targets: List[TargetScheduleInput] = Field(..., min_items=1)

class TargetResponse(BaseModel):
    id: int
    platform: str
    account_id: Optional[int]
    scheduled_at_utc: datetime
    scheduled_at_ist: str
    status: str
    attempts: int
    next_retry_at_ist: Optional[str]
    external_post_id: Optional[str]
    error_message: Optional[str]

class PostDetailResponse(BaseModel):
    id: int
    title: Optional[str]
    body: str
    media_path: Optional[str]
    created_at_ist: str
    targets: List[TargetResponse]
```

---

## 5. Platform Adapter Architecture (Adapter Pattern)

Every social media platform is encapsulated as an adapter adhering to a strict abstract interface. The rest of the application interacts solely through this interface.

### Adapter Interface Signature

```python
# app/adapters/base.py
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Dict, Any

@dataclass(frozen=True)
class PostDTO:
    """Read-only data transfer object passed to adapters."""
    id: int
    title: Optional[str]
    body: str
    media_path: Optional[str]

@dataclass(frozen=True)
class TargetDTO:
    """Read-only target context."""
    id: int
    platform: str
    account_id: Optional[int]

@dataclass(frozen=True)
class PublishResult:
    """Standardized response from platform adapters."""
    success: bool
    external_post_id: Optional[str] = None
    error_message: Optional[str] = None
    raw_response: Optional[Dict[str, Any]] = None

class BasePlatformAdapter(ABC):
    """
    Common contract for all social media adapters.
    Adding a new platform requires implementing only this interface.
    """

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """Unique identifier matching accounts.platform and post_targets.platform."""
        pass

    @abstractmethod
    def publish(
        self,
        post: PostDTO,
        target: TargetDTO,
        credentials: Dict[str, Any]
    ) -> PublishResult:
        """
        Executes the publication to the external platform.
        Must catch platform-specific exceptions and return a PublishResult(success=False, error_message=...)
        rather than bubbling unhandled HTTP/SDK crashes.
        """
        pass

    @abstractmethod
    def validate_credentials(self, credentials: Dict[str, Any]) -> bool:
        """Verifies if the credentials dictionary possesses all mandatory keys."""
        pass
```

### Adapter Registry (Factory Pattern)

```python
# app/adapters/registry.py
from typing import Dict, Type
from app.adapters.base import BasePlatformAdapter

class AdapterRegistry:
    _registry: Dict[str, BasePlatformAdapter] = {}

    @classmethod
    def register(cls, adapter: BasePlatformAdapter) -> None:
        cls._registry[adapter.platform_name.lower()] = adapter

    @classmethod
    def get(cls, platform: str) -> BasePlatformAdapter:
        adapter = cls._registry.get(platform.lower())
        if not adapter:
            raise ValueError(f"No adapter registered for platform: '{platform}'")
        return adapter
```

---

## 6. Scheduler & Retry State Machine

The scheduler runs in-process using APScheduler's `BackgroundScheduler`. Every 60 seconds, it invokes `poll_and_dispatch()`.

### State Transition Diagram

```mermaid
stateDiagram-v2
    [*] --> pending : Post Target Created (scheduled_at in UTC)

    pending --> processing : Poller selects (scheduled_at <= now OR next_retry_at <= now)\nAtomic DB Update: status = 'processing'
    
    processing --> posted : Adapter publish() SUCCESS\nexternal_post_id recorded\nLog level: INFO
    
    processing --> pending : Adapter publish() FAILED & attempts < 3\nattempts += 1\nnext_retry_at = now + backoff\nLog level: WARNING
    
    processing --> failed : Adapter publish() FAILED & attempts >= 3\nattempts = 3\nerror_message recorded\nLog level: ERROR

    posted --> [*]
    failed --> [*]

    failed --> pending : Manual Retry Action (/api/targets/{id}/retry)
    processing --> pending : Startup Crash Recovery (Interrupted job rescued)
```

### State Machine Transition Rules

1. **`pending` -> `processing` (Atomic Claim):**
   - Query selects targets where `status == 'pending'` AND:
     `((next_retry_at IS NULL AND scheduled_at <= now_utc) OR (next_retry_at IS NOT NULL AND next_retry_at <= now_utc))`
   - Atomic database update:
     ```sql
     UPDATE post_targets 
     SET status = 'processing' 
     WHERE id = :target_id AND status = 'pending';
     ```
   - Only targets that successfully updated 1 row are processed. This completely prevents race conditions or duplicate posts.

2. **`processing` -> `posted` (Successful Completion):**
   - Triggered when `adapter.publish()` returns `PublishResult(success=True)`.
   - `status = 'posted'`
   - `external_post_id = result.external_post_id`
   - `error_message = None`
   - Insert row into `logs` (`level='INFO'`, `message='Successfully published to {platform}. External ID: {external_post_id}'`).

3. **`processing` -> `pending` (Transient Failure & Retry Backoff):**
   - Triggered when `adapter.publish()` returns `PublishResult(success=False)` AND `attempts + 1 < 3`.
   - New attempts count: `attempts = attempts + 1`.
   - Backoff delay calculation:
     - **Attempt 1 failure:** Delay = 5 minutes (`next_retry_at = now_utc + 5 minutes`)
     - **Attempt 2 failure:** Delay = 15 minutes (`next_retry_at = now_utc + 15 minutes`)
   - `status = 'pending'`
   - `error_message = result.error_message`
   - Insert row into `logs` (`level='WARNING'`, `message='Attempt {attempts} failed: {error}. Retrying at {next_retry_at_ist}'`).

4. **`processing` -> `failed` (Terminal Failure):**
   - Triggered when `adapter.publish()` returns `PublishResult(success=False)` AND `attempts + 1 >= 3`.
   - `attempts = 3`
   - `status = 'failed'`
   - `next_retry_at = None`
   - `error_message = result.error_message`
   - Insert row into `logs` (`level='ERROR'`, `message='Terminal failure after 3 attempts. Error: {error}'`).

5. **`processing` -> `pending` (Crash Recovery on Startup):**
   - If the application crashes or is terminated while an adapter is executing, targets remain stuck in `'processing'`.
   - On application startup, `app.scheduler.recovery.recover_orphaned_jobs()` executes:
     - Scans for all targets with `status == 'processing'`.
     - Logs warning: `"System restart detected orphaned target {id}. Rescheduling with retry."`
     - Increments `attempts += 1`. If `attempts < 3`, sets status back to `'pending'` with `next_retry_at = now_utc + 1 minute`. If `attempts >= 3`, sets status to `'failed'`.

---

## 7. Dependencies List (`requirements.txt`)

```text
# Web Framework & Server
fastapi==0.110.0
uvicorn[standard]==0.28.0
python-multipart==0.0.9

# Data Validation & Settings
pydantic==2.6.4
pydantic-settings==2.2.1

# Database ORM
SQLAlchemy==2.0.28

# Scheduling
apscheduler==3.10.4

# Secrets & Cryptography
python-dotenv==1.0.1
cryptography==42.0.5

# Timezones & Utilities
pytz==2024.1

# HTTP Clients (for Telegram & Instagram Graph API)
httpx==0.27.0

# Reddit Official API Client
praw==7.7.1

# Web Dashboard (Phase 6)
jinja2==3.1.3

# Testing Suite
pytest==8.1.1
pytest-asyncio==0.23.5
```

---

## 8. Environment Variables (`.env.example`)

```bash
# ==============================================================================
# SOCIAL MEDIA POST SCHEDULER - CONFIGURATION TEMPLATE
# Copy to .env and fill in your values. Never commit .env to version control!
# ==============================================================================

# Application & Environment
APP_ENV=development
DEBUG=True
PORT=8000

# Security (Generate using: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
APP_SECRET_KEY=PLACEHOLDER_FERNET_KEY_GENERATE_WITH_ABOVE_COMMAND

# Database Configuration (SQLite default; change to postgresql+psycopg2://... for Postgres)
DATABASE_URL=sqlite:///./social_scheduler.db

# Timezone Configuration (DB always UTC; this is used for presentation and user input)
DISPLAY_TIMEZONE=Asia/Kolkata

# Scheduler Settings
SCHEDULER_POLL_INTERVAL_SECONDS=60
SCHEDULER_MAX_RETRIES=3

# Media Storage
MEDIA_UPLOAD_DIR=./media
MAX_MEDIA_SIZE_MB=50

# ------------------------------------------------------------------------------
# Default Platform Credentials (Optional fallback if not stored in accounts table)
# ------------------------------------------------------------------------------

# Telegram Bot API (Phase 2)
# Create bot via @BotFather on Telegram to get token. Chat ID can be channel @channelname or group ID
TELEGRAM_BOT_TOKEN=
TELEGRAM_DEFAULT_CHAT_ID=

# Reddit API via PRAW (Phase 5)
# Create script app at https://www.reddit.com/prefs/apps
REDDIT_CLIENT_ID=
REDDIT_CLIENT_SECRET=
REDDIT_USER_AGENT=python:social-scheduler:v1.0.0 (by /u/YOUR_REDDIT_USERNAME)
REDDIT_USERNAME=
REDDIT_PASSWORD=

# Instagram Graph API (Phase 7)
# Requires Meta Developer App, Instagram Business/Creator Account connected to Facebook Page
INSTAGRAM_ACCESS_TOKEN=
INSTAGRAM_BUSINESS_ACCOUNT_ID=
```

---

## 9. Phased Implementation Roadmap

Each phase builds directly upon the previous one with clear, verifiable boundaries.

```mermaid
flowchart TD
    P1["Phase 1: Project Setup, FastAPI, SQLite & Models"] --> P2["Phase 2: Telegram Adapter & Manual Test Publish"]
    P2 --> P3["Phase 3: APScheduler & Automated Publishing"]
    P3 --> P4["Phase 4: Retries, Logs & Status Endpoints"]
    P4 --> P5["Phase 5: Reddit Adapter Integration"]
    P5 --> P6["Phase 6: Web Dashboard (HTML/Jinja2)"]
    P6 --> P7["Phase 7: Instagram Graph API Adapter"]
```

---

### Phase 1: Project Setup, FastAPI Base, SQLite & Models
- **Goal:** Establish repo structure, load configuration, initialize database connection, create ORM models and tables, verify FastAPI starts up and returns health check.
- **Files to Create:**
  - `requirements.txt`
  - `.env.example`, `.env`, `.gitignore`
  - `app/config.py`
  - `app/database.py`
  - `app/models/post.py`, `app/models/target.py`, `app/models/account.py`, `app/models/log.py`
  - `app/schemas/post.py`, `app/schemas/target.py`, `app/schemas/account.py`
  - `app/services/crypto_service.py`
  - `app/api/deps.py`
  - `main.py`
  - `tests/conftest.py`, `tests/unit/test_crypto.py`
- **How to Test This Phase:**
  1. Activate virtual environment: `.\venv\Scripts\activate` (or `./social-scheduler/venv/Scripts/activate`).
  2. Install dependencies: `pip install -r requirements.txt`.
  3. Generate Fernet key and place in `.env`.
  4. Run application: `uvicorn main:app --reload`.
  5. Check `http://127.0.0.1:8000/health`: expect `{"status": "ok", "scheduler_running": false}`.
  6. Inspect SQLite database file `social_scheduler.db` using sqlite3 CLI or python to confirm `posts`, `post_targets`, `accounts`, and `logs` tables exist.
  7. Run pytest: `pytest tests/unit/test_crypto.py` passes.

---

### Phase 2: Telegram Adapter & Manual Test Publish
- **Goal:** Implement the Adapter interface, build the Telegram Bot adapter (supporting text-only, photo, and video), and provide an endpoint to immediately test publish a post to Telegram without scheduler.
- **Files to Create:**
  - `app/adapters/base.py` (BasePlatformAdapter, PostDTO, TargetDTO, PublishResult)
  - `app/adapters/registry.py` (AdapterRegistry)
  - `app/adapters/telegram.py` (TelegramAdapter using httpx to call Telegram Bot API `sendMessage`, `sendPhoto`, `sendVideo`)
  - `app/services/post_service.py` (publish_target_now logic)
  - `app/api/posts.py` (POST `/api/posts`, POST `/api/posts/{id}/publish-now`)
  - `tests/unit/test_telegram_adapter.py`
- **How to Test This Phase:**
  1. Set real or mock `TELEGRAM_BOT_TOKEN` and `TELEGRAM_DEFAULT_CHAT_ID` in `.env`.
  2. Create a test post via `POST /api/posts`:
     ```json
     {
       "body": "Hello from Social Media Scheduler! 🚀",
       "targets": [{"platform": "telegram", "scheduled_at": "2026-10-30T10:00:00+05:30"}]
     }
     ```
  3. Call `POST /api/posts/1/publish-now`.
  4. Verify the message is delivered to the Telegram channel/chat.
  5. Check `post_targets` table: status updated to `"posted"`, `external_post_id` populated with Telegram message ID.
  6. Run automated test with mocked httpx client: `pytest tests/unit/test_telegram_adapter.py`.

---

### Phase 3: APScheduler & Automated Publishing
- **Goal:** Integrate APScheduler into the FastAPI application lifespan. Every 60 seconds, the worker checks the database for pending posts whose `scheduled_at <= now_utc`, atomically transitions them to `processing`, and calls the adapter.
- **Files to Create:**
  - `app/scheduler/engine.py` (Lifespan scheduler start and shutdown)
  - `app/scheduler/worker.py` (Atomic polling, database row lock, target dispatch)
  - `app/scheduler/recovery.py` (Startup orphaned job cleaner)
  - `tests/integration/test_scheduler_worker.py`
- **How to Test This Phase:**
  1. Start application: `uvicorn main:app`.
  2. Confirm console shows: `INFO: Scheduler started. Polling every 60 seconds.`
  3. Schedule a post for 1 minute into the future (in IST):
     ```json
     {
       "body": "Automated schedule test! ⏰",
       "targets": [{"platform": "telegram", "scheduled_at": "<CURRENT_TIME_PLUS_1_MIN>"}]
     }
     ```
  4. Wait 60 seconds. Observe worker log: `Polling pending targets... Found 1 targets due. Processing target #...`.
  5. Verify message arrives automatically on Telegram.
  6. Query `GET /api/targets`: status is `"posted"`.
  7. Run integration test: `pytest tests/integration/test_scheduler_worker.py`.

---

### Phase 4: Retries, Logs & Status Endpoints
- **Goal:** Implement the full failure-handling state machine (5 min -> 15 min -> 60 min -> terminal failure after 3 attempts), database-backed audit logs, and complete inspection endpoints.
- **Files to Create:**
  - `app/services/log_service.py` (Helper to write to `logs` table)
  - `app/api/targets.py` (GET `/api/targets`, GET `/api/targets/{id}`, POST `/api/targets/{id}/retry`)
  - `app/api/logs.py` (GET `/api/logs`)
  - `tests/unit/test_state_machine.py`
- **How to Test This Phase:**
  1. Create a target with invalid Telegram credentials to induce a simulated failure.
  2. Trigger dispatch.
  3. Verify console and database: target status transitions from `processing` back to `pending`.
  4. Inspect `attempts == 1`, `next_retry_at == now_utc + 5 minutes`, `error_message` contains platform error.
  5. Check `GET /api/targets/{id}`: returns attempt count, next retry time in IST, and error message.
  6. Check `GET /api/logs?target_id={id}`: returns level `"WARNING"` log entry.
  7. Manually simulate 3rd failure: status transitions to `"failed"`.
  8. Call `POST /api/targets/{id}/retry`: status resets to `"pending"` and can be executed again.
  9. Run test: `pytest tests/unit/test_state_machine.py`.

---

### Phase 5: Reddit Adapter Integration
- **Goal:** Implement the Reddit adapter using official **PRAW** (Python Reddit API Wrapper). Support text submissions (`submit`) and link/image submissions to designated subreddits.
- **Files to Create:**
  - `app/adapters/reddit.py` (RedditAdapter wrapping PRAW)
  - `tests/unit/test_reddit_adapter.py`
- **How to Test This Phase:**
  1. Add Reddit script credentials to `.env` or create an account via `POST /api/accounts`.
  2. Schedule a post with targets for both Telegram and Reddit:
     ```json
     {
       "title": "Automated Reddit Scheduler Test",
       "body": "This post was scheduled and submitted via our local Python scheduler!",
       "targets": [
         {"platform": "telegram", "scheduled_at": "<TIME>"},
         {"platform": "reddit", "scheduled_at": "<TIME>"}
       ]
     }
     ```
  3. Let the scheduler run.
  4. Check the targeted subreddit (e.g., `r/test` or user profile) to verify submission.
  5. Verify `external_post_id` contains the Reddit post ID or submission URL.
  6. Run test with mocked PRAW client: `pytest tests/unit/test_reddit_adapter.py`.

---

### Phase 6: Web Dashboard (HTML / Jinja2)
- **Goal:** Provide a local, user-friendly HTML dashboard to compose posts, upload media, view a calendar/list view of scheduled items, and inspect real-time statuses and execution logs without raw curl/Postman calls.
- **Files to Create:**
  - `app/api/dashboard.py` (HTML view routes)
  - `app/templates/base.html` (Modern clean layout, navbar, mobile-responsive styling)
  - `app/templates/dashboard.html` (Overview of scheduled, posted, and failed targets with retry buttons)
  - `app/templates/create_post.html` (Form with title, text body, media file upload, platform checkboxes, datetime picker in IST)
  - `app/static/css/styles.css`
  - `app/static/js/main.js`
- **How to Test This Phase:**
  1. Open browser at `http://127.0.0.1:8000/dashboard`.
  2. Click **Create Post**. Fill out title, body, select Telegram and Reddit, pick a time 5 minutes from now in IST, and click Submit.
  3. Confirm the scheduled item appears in the list view with a countdown / scheduled IST time.
  4. Inspect target details and view live log entries directly on the web page.
  5. Test manual "Publish Now" button from dashboard.

---

### Phase 7: Instagram Graph API Adapter
- **Goal:** Implement the Instagram adapter using the official **Meta Instagram Graph API** (Content Publishing API for Instagram Professional/Business Accounts). Support single image/video publishing via the two-step container creation and publish flow.
- **Files to Create:**
  - `app/adapters/instagram.py` (InstagramAdapter using httpx)
  - `tests/unit/test_instagram_adapter.py`
- **How to Test This Phase:**
  1. Set `INSTAGRAM_ACCESS_TOKEN` and `INSTAGRAM_BUSINESS_ACCOUNT_ID`.
  2. Test Container Creation step (`POST /{ig-user-id}/media?image_url=...&caption=...`).
  3. Test Container Publishing step (`POST /{ig-user-id}/media_publish?creation_id=...`).
  4. Handle Meta token expiry checks and report actionable error messages if tokens are expired.
  5. Verify status and external media permalink saved to `post_targets`.
  6. Run test with mocked Meta Graph API responses: `pytest tests/unit/test_instagram_adapter.py`.

---

## 10. Risks, Edge Cases & Mitigation Strategies

| Risk / Edge Case | Likelihood | Impact | Concrete Mitigation Strategy |
|---|---|---|---|
| **App Restart / Crash Mid-Processing** | Medium | High | Any target in status `processing` when the app dies could become stuck indefinitely. **Mitigation:** On application startup, `app.scheduler.recovery.recover_orphaned_jobs()` scans for orphaned `processing` records. If found, it increments the attempt count and resets status to `pending` with a 1-minute retry window. |
| **Race Conditions & Duplicate Posting** | High | Critical | If scheduler poller takes longer than 60s, a subsequent poll might grab the same post. **Mitigation:** (1) APScheduler configured with `max_instances=1` and `coalesce=True`. (2) Atomic SQL conditional update: `UPDATE post_targets SET status = 'processing' WHERE id = :id AND status = 'pending'`. Only the worker that successfully flips the row from `pending` to `processing` executes the adapter. |
| **Timezone Confusion & Drift** | High | High | User schedules in IST (`+05:30`), server runs in UTC or Windows local timezone, causing posts to fire 5.5 hours early or late. **Mitigation:** Strict architecture rule: All internal calculations, comparisons, and database columns store explicit UTC (`timezone=True`). Input schemas convert any localized datetime string into UTC immediately upon ingestion. Datetimes are formatted to `Asia/Kolkata` only when rendering JSON response fields or HTML templates. |
| **Instagram / Meta Media Hosting Requirements** | High | Medium | Instagram Graph API requires public HTTP URLs for images/videos (it cannot accept raw local file bytes directly via multipart upload). **Mitigation:** (1) For local development, provide a configurable public media tunnel helper (e.g. ngrok or Cloudflare Tunnel) or support external image URLs. (2) If a local media path cannot be resolved to a public URL on Instagram, fail fast with a descriptive log before attempting the API call. |
| **OAuth Token Expiration (Instagram / Meta)** | High | Medium | Long-lived page tokens expire after 60 days. Calls will fail with OAuthException 190. **Mitigation:** (1) Store `token_expiry` in `accounts` table. (2) Provide proactive token health check endpoint (`GET /api/accounts/health`). (3) Return clear log: `"Meta OAuth token expired. Please re-authenticate in settings."` |
| **Platform Rate Limits (HTTP 429)** | Medium | Medium | Telegram: ~30 msg/sec; Reddit: ~60 req/min. Exceeding limits triggers bans or timeouts. **Mitigation:** Adapters parse `Retry-After` headers where available. On rate limit, set `next_retry_at = now + retry_after` instead of immediate failure, preserving attempt counts. |
| **Network Timeouts on Media Uploads** | Medium | Low | Large videos or spotty connection cause HTTP timeouts during publishing. **Mitigation:** Configure dedicated HTTP client timeouts (e.g., 60-120 seconds for media uploads instead of default 5s). Catch `httpx.TimeoutException` gracefully and return `PublishResult(success=False, error_message="Network timeout during media upload.")`. |

---

## 11. Verification & Acceptance Checklist

Before proceeding to any code implementation in future steps, the following criteria must be satisfied:
- [x] Architecture respects Adapter Pattern (adding a platform = adding 1 file).
- [x] Database schema is fully specified with relationships and constraints.
- [x] API contract, requests, and responses are fully outlined.
- [x] APScheduler polling and retry backoff state machine are clearly detailed.
- [x] Security considerations (Fernet credential encryption, no hardcoded secrets) are respected.
- [x] All 7 development phases are scoped with concrete testing steps.
- [x] Risks and mitigations are documented.

---
*End of Implementation Plan. Awaiting user review and approval.*

