# AGENTS.md - aria2 Controller

> For AI coding agents operating in this repository.

## Project Overview

aria2 download controller with multi-user support:

- **Backend**: FastAPI (Python 3.12+) with SQLite
- **Frontend**: Next.js 14 (TypeScript) with static export
- **Package managers**: `uv` (backend), `bun` (frontend)
- **Key Features**: User isolation, quota management, task sorting/filtering, batch operations, file management, dynamic configuration, peak metrics tracking, Chinese UI

---

## Quick Reference

| Task | Command |
|------|---------|
| Install all deps | `make install` |
| Build frontend → static | `make build` |
| Run dev server | `make run` |
| Clean | `make clean` |

---

## Build Commands

### Full Stack

```bash
# Install dependencies (both backend & frontend)
make install

# Build frontend and copy to backend/static
make build

# Start server (port 8000, serves API + static frontend)
make run
```

### Backend Only

```bash
# Install Python dependencies
uv sync

# Run with hot-reload
PYTHONPATH=backend uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### Frontend Only

```bash
cd frontend

# Install dependencies
bun install

# Dev server (port 3000)
bun run dev

# Production build (static export to ./out)
bun run build
```

---

## Testing

> No test framework is currently configured. Tests should be added using `pytest` for backend and appropriate tools for frontend.

When tests are added:

```bash
# Backend (future)
uv run pytest backend/tests/
uv run pytest backend/tests/test_foo.py -v           # single file
uv run pytest backend/tests/test_foo.py::test_bar -v # single test
```

---

## Project Structure

```
aria2_controler/
├── backend/
│   ├── app/
│   │   ├── main.py           # FastAPI app, middleware, routers
│   │   ├── auth.py           # Session auth (cookie-based)
│   │   ├── db.py             # SQLite helpers (raw SQL, no ORM)
│   │   ├── schemas.py        # Pydantic request/response models
│   │   ├── core/
│   │   │   ├── config.py     # pydantic-settings configuration
│   │   │   ├── security.py   # Password hashing
│   │   │   └── state.py      # App state management
│   │   ├── routers/          # API endpoints by domain
│   │   │   ├── auth.py       # Login/logout/me
│   │   │   ├── users.py      # User CRUD (admin)
│   │   │   ├── tasks.py      # Task management
│   │   │   ├── files.py      # File browser
│   │   │   ├── stats.py      # System stats
│   │   │   ├── config.py     # System config (admin)
│   │   │   ├── hooks.py      # aria2 callbacks
│   │   │   └── ws.py         # WebSocket endpoint
│   │   └── aria2/            # aria2 RPC client
│   │       ├── client.py     # JSON-RPC client
│   │       └── sync.py       # Background sync task
│   ├── aria2/                # aria2 configuration files
│   │   ├── aria2.conf        # aria2 config
│   │   └── start.sh          # aria2 startup script
│   ├── data/                 # SQLite DB, credentials (gitignored)
│   ├── static/               # Built frontend (gitignored)
│   ├── scripts/              # aria2 hook scripts
│   └── downloads/            # User download directories
├── frontend/
│   ├── app/                  # Next.js App Router pages
│   ├── components/           # React components
│   ├── lib/                  # API client, utilities
│   └── types.ts              # TypeScript type definitions
├── Makefile
├── pyproject.toml
└── uv.lock
```

---

## Code Style Guidelines

### Python (Backend)

**Imports** - Group in order: stdlib → third-party → local

```python
from __future__ import annotations  # Always first if used

import asyncio
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import require_user
from app.db import execute, fetch_one
```

**Type Hints** - Always use, prefer `|` over `Union`

```python
def get_user(user_id: int) -> dict | None:
    ...

async def create_task(uri: str, options: dict | None = None) -> dict:
    ...
```

**Docstrings** - Chinese allowed, keep concise

```python
def _check_disk_space() -> tuple[bool, int]:
    """检查磁盘空间是否足够
    
    返回: (是否足够, 剩余空间字节)
    """
```

**Pydantic Models** - For request/response schemas

```python
class TaskCreate(BaseModel):
    """创建任务请求体"""
    uri: str
    options: dict | None = None
```

**Naming**

- Functions/variables: `snake_case`
- Classes: `PascalCase`
- Private helpers: `_prefix`
- Constants: `UPPER_SNAKE_CASE`

**Error Handling**

```python
# Raise HTTPException with Chinese detail messages
raise HTTPException(
    status_code=status.HTTP_404_NOT_FOUND,
    detail="任务不存在"
)

# Catch specific exceptions, log or handle appropriately
try:
    await client.pause(gid)
except Exception:
    pass  # Only acceptable for cleanup operations
```

**Database** - Use raw SQL with parameterized queries

```python
# CORRECT: Parameterized
execute("UPDATE tasks SET status = ? WHERE id = ?", [status, task_id])
fetch_one("SELECT * FROM tasks WHERE id = ?", [task_id])

# WRONG: String interpolation (SQL injection risk)
execute(f"UPDATE tasks SET status = '{status}' WHERE id = {task_id}")
```

### TypeScript (Frontend)

**Imports** - Use path aliases

```typescript
import type { Task, User } from "@/types";
import { api } from "@/lib/api";
import { Sidebar } from "@/components/Sidebar";
```

**Types** - Define in `types.ts`, export with `type` keyword

```typescript
export type User = {
  id: number;
  username: string;
  is_admin: boolean;
};
```

**Components** - Functional with TypeScript props

```typescript
type SidebarProps = {
  expanded: boolean;
  onToggle: () => void;
  user: User | null;
};

export default function Sidebar({ expanded, onToggle, user }: SidebarProps) {
  // ...
}
```

**Client Components** - Mark with `"use client"` directive

```typescript
"use client";

import { useState, useEffect } from "react";
```

**API Calls** - Use the centralized `api` object from `@/lib/api`

```typescript
// CORRECT
const tasks = await api.listTasks();

// WRONG: Direct fetch
const res = await fetch("/api/tasks");
```

---

## Environment Variables

### Backend (via `pydantic-settings`, prefix: `ARIA2C_`)

```bash
# backend/env.example
ARIA2C_APP_NAME=aria2-controler
ARIA2C_DEBUG=true
ARIA2C_DATABASE_PATH=./data/app.db
ARIA2C_SESSION_TTL_SECONDS=43200
ARIA2C_ARIA2_RPC_URL=http://localhost:6800/jsonrpc
ARIA2C_ARIA2_RPC_SECRET=
ARIA2C_ARIA2_POLL_INTERVAL=2.0
ARIA2C_DOWNLOAD_DIR=./downloads
```

### Frontend

```bash
# frontend/env.local.example
NEXT_PUBLIC_API_BASE=http://localhost:8000
```

---

## Architecture Notes

### Authentication

- Cookie-based session authentication (`aria2_session` cookie)
- Sessions stored in SQLite `sessions` table
- Dependencies: `require_user`, `require_admin` in `app/auth.py`
- Current user info via `GET /api/auth/me`

### Database

- SQLite with raw SQL (no ORM)
- Auto-init on startup via `init_db()`
- Default admin created on first run (credentials in `backend/data/admin_credentials.txt`)
- Tables: users (with quota), sessions, tasks (with peak metrics), config

### Frontend Build

- Static export (`output: 'export'` in next.config.mjs)
- Built files copied to `backend/static/`
- Served by FastAPI with custom middleware for SPA routing
- Fully localized Chinese interface

### aria2 Integration

- JSON-RPC client in `app/aria2/client.py`
- Background sync task polls aria2 every 2 seconds
- Tracks peak download speed and peak connections
- WebSocket endpoint for real-time updates
- Automatic .aria2 control file cleanup on task/file deletion
- Dynamic configuration: aria2 RPC settings can be changed via UI without restart

### Key Features Implementation

**User Space Management**

- Each user has isolated download directory: `downloads/{user_id}/`
- User quota stored in database, enforced on task creation
- Dynamic space calculation considers both quota and machine free space
- Space widget shows: used/available (dynamically adjusted)

**Task Management**

- Task sorting: by time, speed, remaining time (asc/desc)
- Task filtering: all, active, complete, error
- Batch operations: select multiple, pause/resume/delete
- Smart deletion: completed tasks can optionally delete files, incomplete tasks always delete files
- Peak metrics: tracks peak_download_speed and peak_connections
- Task caching: prevents aria2 interface blocking during large task creation

**File Management**

- File browser with path validation (prevents directory traversal)
- File extension blacklist (configurable, e.g., .aria2, .tmp, .part)
- Automatic .aria2 cleanup when deleting files
- File operations: list, download, delete, rename

**Dynamic Configuration**

- aria2 RPC URL and secret configurable via UI
- System limits: max_task_size, min_free_disk
- File extension blacklist
- All configs stored in database, hot-reloadable

---

## Common Patterns

### Adding a New API Endpoint

1. Create/modify router in `backend/app/routers/`
2. Define Pydantic schemas if needed
3. Use `require_user` or `require_admin` dependency
4. Register router in `main.py`

### Adding a New Frontend Page

1. Create page in `frontend/app/{route}/page.tsx`
2. Add to sidebar if needed in `frontend/components/Sidebar.tsx`
3. Add URL alias in `main.py` for static export routing
4. Run `make build` to update static files

---

## Don'ts

- **Don't** use `as any`, `@ts-ignore`, or `@ts-expect-error`
- **Don't** modify tests to match broken code
- **Don't** use string interpolation in SQL queries
- **Don't** create new files when editing existing ones suffices
- **Don't** add features not explicitly requested
- **Don't** commit `backend/data/` or `backend/static/` contents
