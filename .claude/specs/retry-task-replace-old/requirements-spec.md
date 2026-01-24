# Retry Task and Replace Old - Technical Specification

## Problem Statement

- **Business Issue**: When a download task fails, users cannot easily retry the same download. Current retry simply creates a new task while keeping the old failed task in the list, causing confusion and duplicate entries.
- **Current State**: The `retryTask` function in `tasks/page.tsx` calls `api.createTask(task.uri)` which creates a new task but leaves the old failed task. Torrent tasks cannot be retried at all.
- **Expected Outcome**: A single API call that creates a new aria2 task using the original URI, deletes the old task record on success (keeping downloaded files), and returns the new task info. Failed retries keep the old task unchanged.

---

## Solution Overview

- **Approach**: Add a new backend endpoint `POST /tasks/{id}/retry` that atomically handles retry logic: validate task eligibility, create new aria2 task, delete old record on success.
- **Core Changes**:
  1. Backend: New endpoint in `tasks.py`
  2. Frontend API: New `retryTask(id)` method in `api.ts`
  3. Frontend UI: Update retry handlers in `tasks/page.tsx` and `history/page.tsx`
- **Success Criteria**:
  - Retry replaces old task record with new task
  - Old files remain on disk
  - Torrent tasks return descriptive error
  - Failed aria2 creation keeps old task

---

## Technical Implementation

### Database Changes

No schema changes required. The retry operation uses existing `tasks` table:
- Read old task record
- Insert new task record (via existing `create_task` flow)
- Delete old task record on success

### Code Changes

#### 1. Backend: `backend/app/routers/tasks.py`

**New endpoint to add after line 538 (after `update_task_status` function):**

```python
@router.post("/{task_id}/retry")
async def retry_task(
    task_id: int,
    request: Request,
    user: dict = Depends(require_user)
) -> dict:
    """Retry a failed task by creating new download and removing old record

    Prerequisites:
    - Task must exist and belong to current user
    - Task must NOT be a torrent task (uri != "[torrent]")

    On success:
    - Creates new aria2 task with original URI
    - Deletes old task record (files remain on disk)
    - Returns new task info

    On failure:
    - Old task remains unchanged
    - Returns error details
    """
    # 1. Fetch and validate task
    task = fetch_one(
        "SELECT * FROM tasks WHERE id = ? AND owner_id = ?",
        [task_id, user["id"]]
    )
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    # 2. Check if torrent task
    if task["uri"] == "[torrent]":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Torrent tasks cannot be retried. Please re-upload the torrent file."
        )

    # 3. Reuse create_task logic for validation and task creation
    original_uri = task["uri"]

    # Check disk space
    disk_ok, disk_free = _check_disk_space()
    if not disk_ok:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Insufficient disk space, remaining {disk_free / 1024 / 1024 / 1024:.2f} GB"
        )

    # Check file size (HTTP/HTTPS)
    max_size = get_max_task_size()
    file_size = await _check_url_size(original_uri)
    if file_size is not None and file_size > max_size:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"File size {file_size / 1024 / 1024 / 1024:.2f} GB exceeds limit {max_size / 1024 / 1024 / 1024:.2f} GB"
        )

    # Check user quota
    user_available = _get_user_available_space(user)
    if file_size is not None and file_size > user_available:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"File size {file_size / 1024 / 1024 / 1024:.2f} GB exceeds your available space {user_available / 1024 / 1024 / 1024:.2f} GB"
        )

    # 4. Create new task record
    user_dir = _get_user_download_dir(user["id"])
    options = {"dir": user_dir}

    new_task_id = execute(
        """
        INSERT INTO tasks (owner_id, uri, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        [user["id"], original_uri, "queued", utc_now(), utc_now()],
    )

    state = _get_state(request)
    async with state.lock:
        state.pending_tasks[new_task_id] = {"uri": original_uri}

    # 5. Add to aria2 and handle result
    client = _get_client(request)
    try:
        gid = await client.add_uri([original_uri], options)
        execute(
            "UPDATE tasks SET gid = ?, status = ?, updated_at = ? WHERE id = ?",
            [gid, "active", utc_now(), new_task_id]
        )

        # 6. Success: Clean up old task from aria2 if it has gid
        if task.get("gid"):
            try:
                await client.force_remove(task["gid"])
            except Exception:
                pass
            try:
                await client.remove_download_result(task["gid"])
            except Exception:
                pass

        # 7. Delete old task record (keep files on disk)
        execute("DELETE FROM tasks WHERE id = ?", [task_id])

    except Exception as exc:
        # Aria2 failed: mark new task as error, keep old task
        execute(
            "UPDATE tasks SET status = ?, error = ?, updated_at = ? WHERE id = ?",
            ["error", str(exc), utc_now(), new_task_id],
        )
        # Delete the failed new task
        execute("DELETE FROM tasks WHERE id = ?", [new_task_id])
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create aria2 task: {exc}"
        )
    finally:
        async with state.lock:
            state.pending_tasks.pop(new_task_id, None)

    # 8. Return new task info
    new_task = fetch_one("SELECT * FROM tasks WHERE id = ?", [new_task_id])
    await broadcast_update(state, user["id"], new_task_id)
    return new_task
```

#### 2. Frontend API: `frontend/lib/api.ts`

**Add new method after `changeTaskPosition` (around line 87):**

```typescript
retryTask: (id: number) =>
  request<Task>(`/api/tasks/${id}/retry`, {
    method: "POST",
  }),
```

#### 3. Frontend: `frontend/app/tasks/page.tsx`

**Modify the `retryTask` function (lines 621-634):**

Replace existing implementation:
```typescript
async function retryTask(task: Task) {
  // Torrent tasks cannot be retried
  if (task.uri === "[torrent]") {
    alert("Torrent tasks cannot be retried directly. Please re-upload the torrent file.");
    return;
  }

  try {
    const newTask = await api.retryTask(task.id);
    // Replace old task with new task in the list
    setTasks((prev) => {
      const filtered = prev.filter((t) => t.id !== task.id);
      return [newTask, ...filtered];
    });
  } catch (err) {
    alert("Retry failed: " + (err as Error).message);
  }
}
```

**Modify `batchRetryTasks` function (lines 636-671):**

Replace existing implementation:
```typescript
async function batchRetryTasks() {
  if (selectedTasks.size === 0) return;

  // Filter retryable error tasks (exclude torrent tasks)
  const errorTasks = tasks.filter(
    (t) =>
      selectedTasks.has(t.id) &&
      t.status === "error" &&
      t.uri !== "[torrent]"
  );

  if (errorTasks.length === 0) {
    alert("No retryable tasks (torrent tasks need to be re-uploaded)");
    return;
  }

  let successCount = 0;
  let failCount = 0;
  const newTasks: Task[] = [];
  const retriedIds: number[] = [];

  for (const task of errorTasks) {
    try {
      const newTask = await api.retryTask(task.id);
      newTasks.push(newTask);
      retriedIds.push(task.id);
      successCount++;
    } catch (err) {
      failCount++;
      console.error(`Failed to retry task ${task.id}:`, err);
    }
  }

  // Update task list: remove old tasks, add new tasks
  setTasks((prev) => {
    const filtered = prev.filter((t) => !retriedIds.includes(t.id));
    return [...newTasks, ...filtered];
  });

  if (failCount > 0) {
    alert(`Retry complete: ${successCount} succeeded, ${failCount} failed`);
  } else if (successCount > 0) {
    alert(`Successfully retried ${successCount} tasks`);
  }
}
```

#### 4. Frontend: `frontend/app/history/page.tsx`

**Add retry functionality to history page.**

**Add import for Task type (already imported).**

**Add retry handler function after `handleClearAll` (around line 80):**

```typescript
async function handleRetry(task: Task) {
  // Torrent tasks cannot be retried
  if (task.uri === "[torrent]") {
    alert("Torrent tasks cannot be retried directly. Please re-upload the torrent file.");
    return;
  }

  try {
    await api.retryTask(task.id);
    // Remove from history list (task moved to active tasks)
    setTasks((prev) => prev.filter((t) => t.id !== task.id));
    alert("Task retry initiated. Check the Tasks page for progress.");
  } catch (err) {
    alert("Retry failed: " + (err as Error).message);
  }
}
```

**Add retry button in the table row (around line 346-358):**

Modify the action cell to include retry button for error tasks:

```tsx
<td style={{ padding: "12px 16px", textAlign: "right" }}>
  <div style={{ display: "flex", gap: "8px", justifyContent: "flex-end" }}>
    {task.status === "error" && (
      <button
        className="button secondary"
        onClick={() => handleRetry(task)}
        style={{
          padding: "4px 12px",
          fontSize: 12,
          height: 28,
        }}
      >
        Retry
      </button>
    )}
    <button
      className="button secondary danger"
      onClick={() => handleDelete(task.id)}
      style={{
        padding: "4px 12px",
        fontSize: 12,
        height: 28,
      }}
    >
      Delete
    </button>
  </div>
</td>
```

### API Specification

#### POST /api/tasks/{task_id}/retry

**Request:**
- Method: `POST`
- Path Parameter: `task_id` (integer) - ID of the task to retry
- Authentication: Required (session cookie)
- Body: None

**Success Response (200 OK):**
```json
{
  "id": 15,
  "owner_id": 1,
  "gid": "a1b2c3d4e5f6",
  "uri": "https://example.com/file.zip",
  "status": "active",
  "name": null,
  "total_length": 0,
  "completed_length": 0,
  "download_speed": 0,
  "upload_speed": 0,
  "error": null,
  "created_at": "2026-01-23T10:00:00Z",
  "updated_at": "2026-01-23T10:00:00Z",
  "artifact_path": null,
  "artifact_token": null
}
```

**Error Responses:**

| Status | Condition | Response Body |
|--------|-----------|---------------|
| 404 | Task not found or not owned by user | `{"detail": "Task not found"}` |
| 400 | Task is a torrent task | `{"detail": "Torrent tasks cannot be retried. Please re-upload the torrent file."}` |
| 403 | Disk space insufficient | `{"detail": "Insufficient disk space, remaining X.XX GB"}` |
| 403 | File size exceeds limit | `{"detail": "File size X.XX GB exceeds limit Y.YY GB"}` |
| 403 | User quota exceeded | `{"detail": "File size X.XX GB exceeds your available space Y.YY GB"}` |
| 500 | aria2 connection failed | `{"detail": "Failed to create aria2 task: <error>"}` |

### Configuration Changes

None required.

---

## Implementation Sequence

### Phase 1: Backend API
1. Add `retry_task` endpoint to `/Users/easyops/coding/aria2_controler/backend/app/routers/tasks.py`
2. Test endpoint manually with curl:
   ```bash
   curl -X POST http://localhost:8000/api/tasks/1/retry \
     -H "Cookie: session=<session_id>"
   ```

### Phase 2: Frontend API
1. Add `retryTask` method to `/Users/easyops/coding/aria2_controler/frontend/lib/api.ts`

### Phase 3: Tasks Page UI
1. Update `retryTask` function in `/Users/easyops/coding/aria2_controler/frontend/app/tasks/page.tsx`
2. Update `batchRetryTasks` function in the same file

### Phase 4: History Page UI
1. Add `handleRetry` function to `/Users/easyops/coding/aria2_controler/frontend/app/history/page.tsx`
2. Add retry button for error tasks in the table

---

## Validation Plan

### Unit Tests

1. **Backend API Tests:**
   - Test retry with valid HTTP task -> returns new task, old deleted
   - Test retry with torrent task -> returns 400 error
   - Test retry with non-existent task -> returns 404
   - Test retry with other user's task -> returns 404
   - Test retry when aria2 unavailable -> returns 500, old task preserved

### Integration Tests

1. **Full Flow Test:**
   - Create task -> force error -> retry -> verify new task active, old task gone
   - Create torrent task -> force error -> retry -> verify error message

2. **UI Flow Test:**
   - Tasks page: Click retry on error task -> task replaced in list
   - History page: Click retry on error task -> task removed from history
   - Batch retry: Select multiple error tasks -> all retried and replaced

### Business Logic Verification

1. **File Preservation:**
   - Retry a partially downloaded task
   - Verify original partial file still exists on disk
   - Verify new task can resume or restart download

2. **Edge Cases:**
   - Retry task with very long URI
   - Retry task when disk space is low
   - Retry task when user quota is near limit
