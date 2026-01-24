# Repository Context Report

## Project Overview

| Property | Value |
|----------|-------|
| Project Type | Multi-user Download Management Platform |
| Architecture | Frontend/Backend Separation |
| Core Function | aria2 Download Controller with Web UI |
| Target Users | Multi-tenant download service users |

---

## Technology Stack

### Backend (Python)

| Component | Technology | Version |
|-----------|------------|---------|
| Framework | FastAPI | 0.111.0 |
| Server | uvicorn | - |
| HTTP Client | aiohttp | 3.9.5 |
| Config | pydantic-settings | 2.3.4 |
| Database | SQLite (native SQL) | - |
| Python | 3.12+ | - |
| Package Manager | uv | - |

### Frontend (TypeScript)

| Component | Technology | Version |
|-----------|------------|---------|
| Framework | Next.js (Static Export) | 14.2.5 |
| UI Library | React | 18.3.1 |
| Language | TypeScript | 5.5.2 |
| Package Manager | Bun | - |
| Drag & Drop | @dnd-kit/core, @dnd-kit/sortable | ^6.3.1, ^10.0.0 |

### External Dependencies

| Component | Purpose |
|-----------|---------|
| aria2c | Download engine with RPC enabled |
| Node.js 18+ | Frontend toolchain |

---

## Directory Structure

```
aria2_controler/
├── backend/
│   ├── app/
│   │   ├── aria2/           # aria2 RPC client & sync
│   │   │   ├── client.py    # Aria2Client class
│   │   │   └── sync.py      # Task status sync & broadcast
│   │   ├── core/            # Core modules
│   │   │   ├── config.py    # Settings
│   │   │   ├── security.py  # Password hashing
│   │   │   ├── state.py     # App state
│   │   │   └── rate_limit.py
│   │   ├── routers/         # API endpoints
│   │   │   ├── auth.py      # Authentication
│   │   │   ├── config.py    # System config
│   │   │   ├── files.py     # File management
│   │   │   ├── hooks.py     # aria2 callbacks
│   │   │   ├── stats.py     # Statistics
│   │   │   ├── tasks.py     # Task management [KEY FILE]
│   │   │   ├── users.py     # User management
│   │   │   └── ws.py        # WebSocket
│   │   ├── auth.py          # Auth utilities
│   │   ├── db.py            # SQLite operations
│   │   ├── main.py          # App entry
│   │   └── schemas.py       # Pydantic models
│   ├── aria2/               # aria2 config & scripts
│   └── data/                # SQLite DB & credentials
├── frontend/
│   ├── app/                 # Next.js pages
│   │   ├── tasks/
│   │   │   ├── page.tsx     # Task list [KEY FILE]
│   │   │   └── detail/page.tsx
│   │   ├── history/page.tsx # History [KEY FILE]
│   │   ├── files/page.tsx
│   │   ├── settings/page.tsx
│   │   └── users/page.tsx
│   ├── components/          # Reusable components
│   │   ├── Sidebar.tsx
│   │   ├── AuthLayout.tsx
│   │   ├── FileList.tsx
│   │   ├── SpeedChart.tsx
│   │   └── StatsWidget.tsx
│   ├── lib/
│   │   ├── api.ts           # API client [KEY FILE]
│   │   ├── AuthContext.tsx
│   │   ├── notification.ts
│   │   └── utils.ts
│   └── types.ts             # TypeScript types [KEY FILE]
├── PRD_Tracking/            # Project tracking docs
├── Makefile                 # Build automation
└── README.md
```

---

## Task Management System (Current Implementation)

### Database Schema (tasks table)

```sql
CREATE TABLE tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    gid TEXT,                          -- aria2 GID
    uri TEXT NOT NULL,                 -- Download URL or "[torrent]"
    status TEXT NOT NULL,              -- queued|active|paused|waiting|complete|error|stopped|removed
    name TEXT,
    total_length INTEGER DEFAULT 0,
    completed_length INTEGER DEFAULT 0,
    download_speed INTEGER DEFAULT 0,
    upload_speed INTEGER DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    artifact_path TEXT,
    artifact_token TEXT,
    peak_download_speed INTEGER DEFAULT 0,
    peak_connections INTEGER DEFAULT 0,
    FOREIGN KEY(owner_id) REFERENCES users(id)
);
```

### Task Status Flow

```
queued -> active -> complete
              \-> paused -> active
              \-> waiting -> active
              \-> error -> (retry?) -> new task
              \-> stopped
         -> removed (soft delete)
```

### Current Retry Implementation

**Location**: `/Users/easyops/coding/aria2_controler/frontend/app/tasks/page.tsx`

**Current Behavior** (lines 621-634):
```typescript
async function retryTask(task: Task) {
  // Torrent tasks cannot be retried
  if (task.uri === "[torrent]") {
    alert("Torrent tasks cannot be retried directly, please re-upload the torrent file");
    return;
  }

  try {
    const newTask = await api.createTask(task.uri);  // Creates NEW task
    setTasks((prev) => [newTask, ...prev]);          // Adds to list
  } catch (err) {
    alert("Retry failed: " + (err as Error).message);
  }
}
```

**Key Observations**:
1. Retry creates a **new task** via `api.createTask(task.uri)`
2. Old failed task remains in the list
3. Batch retry (`batchRetryTasks`) follows the same pattern
4. Torrent tasks (`uri === "[torrent]"`) cannot be retried

### aria2 Client API (Backend)

**Location**: `/Users/easyops/coding/aria2_controler/backend/app/aria2/client.py`

Available methods:
- `add_uri(uris, options)` - Add HTTP/FTP download
- `add_torrent(torrent, uris, options)` - Add torrent download
- `tell_status(gid)` - Get task status
- `pause(gid)` / `unpause(gid)` - Pause/resume
- `remove(gid)` / `force_remove(gid)` - Remove task
- `remove_download_result(gid)` - Clear download result
- `change_position(gid, pos, how)` - Change queue position

**Note**: No native "retry" or "restart" method in aria2 RPC.

### Frontend API Client

**Location**: `/Users/easyops/coding/aria2_controler/frontend/lib/api.ts`

Relevant methods:
- `createTask(uri)` - Create new task
- `uploadTorrent(torrent, options)` - Upload torrent
- `deleteTask(id, deleteFiles)` - Delete task
- `updateTaskStatus(id, status)` - Pause/resume

---

## Integration Points for Retry Feature

### Backend Changes Needed

1. **New Endpoint**: `POST /api/tasks/{id}/retry`
   - Location: `backend/app/routers/tasks.py`
   - Should:
     - Fetch original task by ID
     - Validate task is in retryable state (error, stopped)
     - Create new aria2 task with same URI and options
     - Option: Delete old task OR update old task with new GID

2. **Database Considerations**:
   - Option A: Update existing task record (reuse ID)
   - Option B: Create new task, mark old as "superseded"
   - Option C: Create new task, delete old task

3. **aria2 Integration**:
   - Clear old download result if exists
   - Create new download with `add_uri` or `add_torrent`
   - Handle partially downloaded files

### Frontend Changes Needed

1. **API Client** (`frontend/lib/api.ts`):
   - Add `retryTask(id, options?)` method
   - Options: `{ deleteOld?: boolean, deleteFiles?: boolean }`

2. **Task List Page** (`frontend/app/tasks/page.tsx`):
   - Update `retryTask` function to use new API
   - Update `batchRetryTasks` for batch operations
   - Handle UI state when retrying

3. **History Page** (`frontend/app/history/page.tsx`):
   - Add retry button for error/stopped tasks
   - Handle navigation after retry

### TypeScript Types

**Location**: `/Users/easyops/coding/aria2_controler/frontend/types.ts`

May need new types:
```typescript
export type RetryTaskOptions = {
  deleteOld?: boolean;
  deleteFiles?: boolean;
};

export type RetryTaskResponse = {
  ok: boolean;
  old_task_id: number;
  new_task?: Task;
  message?: string;
};
```

---

## File Handling Patterns

### User Download Directory

```python
# backend/app/routers/tasks.py
def _get_user_download_dir(user_id: int) -> str:
    base = Path(settings.download_dir).resolve()
    user_dir = base / str(user_id)
    user_dir.mkdir(parents=True, exist_ok=True)
    return str(user_dir)
```

Each user has isolated directory: `{download_dir}/{user_id}/`

### Cleanup Pattern

On task deletion:
1. Remove from aria2: `force_remove(gid)` + `remove_download_result(gid)`
2. Clean `.aria2` control file
3. Optionally delete downloaded files
4. Mark task as "removed" in database

---

## Conventions to Follow

### Backend

1. **Router Structure**:
   - Prefix: `/api/tasks`
   - Tags: `["tasks"]`
   - Use Pydantic models for request/response

2. **Error Handling**:
   ```python
   raise HTTPException(
       status_code=status.HTTP_404_NOT_FOUND,
       detail="Task not found"
   )
   ```

3. **Database Operations**:
   - Use `execute()`, `fetch_one()`, `fetch_all()` from `app.db`
   - Use `utc_now()` for timestamps

4. **User Isolation**:
   - Always verify `owner_id` matches current user
   - Use `Depends(require_user)` for auth

### Frontend

1. **API Pattern**:
   ```typescript
   retryTask: (id: number, options?: RetryTaskOptions) =>
     request<RetryTaskResponse>(`/api/tasks/${id}/retry`, {
       method: "POST",
       body: JSON.stringify(options || {}),
     }),
   ```

2. **State Management**:
   - Use `useState` for local state
   - Update via `setTasks(prev => ...)` pattern

3. **Error Display**:
   - Use `alert()` for simple errors
   - Use error state for inline display

4. **Chinese UI Text**:
   - All user-facing text in Chinese
   - Technical terms can be in English

---

## Potential Constraints & Considerations

### Technical Constraints

1. **Torrent Tasks**: Cannot retry directly (no stored torrent data)
   - Need to re-upload torrent file
   - Or store torrent base64 in database

2. **Partial Downloads**:
   - aria2 may resume from `.aria2` control file
   - If deleted, download starts from scratch

3. **File Conflicts**:
   - Same filename may exist from previous attempt
   - Need to handle overwrite or rename

4. **Quota Check**:
   - Must re-check user quota on retry
   - May fail if quota exceeded

### Business Logic Constraints

1. **Replace vs Add**:
   - User expectation: "retry" should not create duplicates
   - Current behavior: creates new task, keeps old

2. **Status Preservation**:
   - Should retry preserve peak_download_speed, peak_connections?
   - Should retry reset error message?

3. **History Tracking**:
   - Should old task be visible in history?
   - Need audit trail of retries?

### UI/UX Constraints

1. **Feedback**:
   - Show retry progress
   - Indicate when retry is in progress

2. **Batch Operations**:
   - Batch retry with option to delete old tasks
   - Progress indicator for batch

3. **Confirmation**:
   - Confirm before retry if files exist
   - Option to delete old files on retry

---

## Related Files Summary

| File | Purpose | Changes Needed |
|------|---------|----------------|
| `backend/app/routers/tasks.py` | Task CRUD API | Add retry endpoint |
| `backend/app/aria2/client.py` | aria2 RPC client | No changes |
| `frontend/lib/api.ts` | API client | Add retryTask method |
| `frontend/app/tasks/page.tsx` | Task list UI | Update retry handlers |
| `frontend/app/history/page.tsx` | History UI | Add retry button |
| `frontend/types.ts` | TS types | Add retry types |

---

## Next Steps

1. Define retry behavior:
   - Option A: Replace old task (update same record)
   - Option B: Create new task, delete old
   - Option C: Create new task, mark old as superseded

2. Design API contract for retry endpoint

3. Implement backend endpoint

4. Update frontend to use new endpoint

5. Handle edge cases (torrent, partial files, quota)
