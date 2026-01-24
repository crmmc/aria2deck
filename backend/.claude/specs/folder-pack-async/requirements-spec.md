# Async Folder Pack Download - Technical Specification

Version: 1.0
Created: 2026-01-23

---

## Problem Statement

- **Business Issue**: Users need to download entire folders but browsers cannot handle folder downloads directly. Current workaround requires users to download files one by one.
- **Current State**: No folder download capability exists. Users must manually download each file in a folder.
- **Expected Outcome**: Users can request a folder to be packed into a ZIP/7Z archive asynchronously, with progress tracking, and download the resulting archive file.

---

## Solution Overview

- **Approach**: Implement async background packing using 7-zip CLI, with space reservation mechanism to prevent over-allocation. Tasks are tracked in database with real-time progress updates via WebSocket.
- **Core Changes**:
  1. New `pack_tasks` database table for tracking pack jobs
  2. New pack settings in `config` table (format, compression level)
  3. New `/api/files/pack` endpoints for CRUD operations
  4. New background service for 7-zip execution and progress tracking
  5. Frontend UI for pack confirmation, task monitoring, and admin settings
- **Success Criteria**:
  - User can initiate folder pack from files page
  - Space is reserved during packing to prevent over-allocation
  - Progress updates are visible in real-time
  - Source folder is deleted after successful pack
  - Admin can configure pack format and compression level

---

## Technical Implementation

### 1. Database Changes

#### 1.1 New Table: `pack_tasks`

Add to `backend/app/db.py` in `init_db()` function:

```python
cur.execute(
    """
    CREATE TABLE IF NOT EXISTS pack_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id INTEGER NOT NULL,
        folder_path TEXT NOT NULL,
        folder_size INTEGER NOT NULL,
        reserved_space INTEGER NOT NULL,
        output_path TEXT,
        output_size INTEGER,
        status TEXT NOT NULL DEFAULT 'pending',
        progress INTEGER DEFAULT 0,
        error_message TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY(owner_id) REFERENCES users(id)
    )
    """
)
conn.commit()
```

**Status values**: `pending` | `packing` | `done` | `failed` | `cancelled`

#### 1.2 New Config Entries

Add default config values in `init_db()`:

```python
cur.execute(
    """
    INSERT OR IGNORE INTO config (key, value) VALUES
    ('pack_format', 'zip'),
    ('pack_compression_level', '5')
    """
)
conn.commit()
```

### 2. Backend Code Changes

#### 2.1 New File: `backend/app/services/pack.py`

```python
"""Async folder packing service using 7-zip CLI"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
from pathlib import Path
from typing import Callable

from app.core.config import settings
from app.db import execute, fetch_one, utc_now
from app.routers.config import get_config_value


class PackTaskManager:
    """Manages async pack task execution"""

    _running_tasks: dict[int, asyncio.subprocess.Process] = {}

    @classmethod
    def get_pack_format(cls) -> str:
        """Get pack format from config (zip or 7z)"""
        val = get_config_value("pack_format")
        return val if val in ("zip", "7z") else "zip"

    @classmethod
    def get_compression_level(cls) -> int:
        """Get compression level (1-9)"""
        val = get_config_value("pack_compression_level")
        try:
            level = int(val) if val else 5
            return max(1, min(9, level))
        except ValueError:
            return 5

    @classmethod
    async def start_pack(
        cls,
        task_id: int,
        user_id: int,
        folder_path: str,
        on_progress: Callable[[int, int], None] | None = None
    ) -> None:
        """Start async packing process

        Args:
            task_id: Pack task database ID
            user_id: Owner user ID
            folder_path: Relative path to folder within user directory
            on_progress: Callback(task_id, progress_percent)
        """
        user_dir = Path(settings.download_dir) / str(user_id)
        source = user_dir / folder_path

        if not source.exists() or not source.is_dir():
            cls._update_task_error(task_id, "Source folder does not exist")
            return

        # Determine output format and path
        pack_format = cls.get_pack_format()
        compression = cls.get_compression_level()
        output_name = f"{source.name}.{pack_format}"
        output_path = user_dir / output_name

        # Ensure unique filename
        counter = 1
        while output_path.exists():
            output_name = f"{source.name}_{counter}.{pack_format}"
            output_path = user_dir / output_name
            counter += 1

        # Update status to packing
        execute(
            "UPDATE pack_tasks SET status = ?, output_path = ?, updated_at = ? WHERE id = ?",
            ["packing", str(output_path), utc_now(), task_id]
        )

        # Build 7z command
        # -tzip or -t7z for format
        # -mx=N for compression level
        # -bsp1 for progress output
        format_flag = f"-t{pack_format}"
        cmd = [
            "7z", "a", format_flag, f"-mx={compression}", "-bsp1",
            str(output_path), str(source) + "/*"
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
            )
            cls._running_tasks[task_id] = process

            # Parse progress from 7z output
            progress = 0
            async for line in process.stdout:
                line_text = line.decode("utf-8", errors="ignore").strip()
                # 7z progress format: " 45%" or similar
                match = re.search(r"(\d+)%", line_text)
                if match:
                    new_progress = int(match.group(1))
                    if new_progress != progress:
                        progress = new_progress
                        execute(
                            "UPDATE pack_tasks SET progress = ?, updated_at = ? WHERE id = ?",
                            [progress, utc_now(), task_id]
                        )
                        if on_progress:
                            on_progress(task_id, progress)

            await process.wait()

            if process.returncode == 0:
                # Success: get output size, delete source, update status
                output_size = output_path.stat().st_size if output_path.exists() else 0

                # Delete source folder
                shutil.rmtree(source)

                execute(
                    """UPDATE pack_tasks SET
                       status = ?, progress = 100, output_size = ?,
                       reserved_space = 0, updated_at = ?
                       WHERE id = ?""",
                    ["done", output_size, utc_now(), task_id]
                )
            else:
                # Failed: cleanup partial output
                if output_path.exists():
                    output_path.unlink()
                cls._update_task_error(task_id, f"7z exited with code {process.returncode}")

        except asyncio.CancelledError:
            # Task was cancelled
            if output_path.exists():
                output_path.unlink()
            execute(
                "UPDATE pack_tasks SET status = ?, reserved_space = 0, updated_at = ? WHERE id = ?",
                ["cancelled", utc_now(), task_id]
            )
        except Exception as exc:
            if output_path.exists():
                output_path.unlink()
            cls._update_task_error(task_id, str(exc))
        finally:
            cls._running_tasks.pop(task_id, None)

    @classmethod
    async def cancel_pack(cls, task_id: int) -> bool:
        """Cancel a running pack task

        Returns True if cancelled, False if not running
        """
        process = cls._running_tasks.get(task_id)
        if process:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                process.kill()
            return True
        return False

    @classmethod
    def _update_task_error(cls, task_id: int, error: str) -> None:
        execute(
            """UPDATE pack_tasks SET
               status = ?, error_message = ?, reserved_space = 0, updated_at = ?
               WHERE id = ?""",
            ["failed", error, utc_now(), task_id]
        )


def calculate_folder_size(path: Path) -> int:
    """Calculate total size of folder in bytes"""
    total = 0
    try:
        for entry in path.rglob("*"):
            if entry.is_file():
                total += entry.stat().st_size
    except Exception:
        pass
    return total


def get_reserved_space() -> int:
    """Get total reserved space from pending/packing tasks"""
    from app.db import fetch_one
    result = fetch_one(
        """SELECT COALESCE(SUM(reserved_space), 0) as total
           FROM pack_tasks
           WHERE status IN ('pending', 'packing')"""
    )
    return result["total"] if result else 0


def get_server_available_space() -> int:
    """Get server available space minus reserved space"""
    download_path = Path(settings.download_dir)
    download_path.mkdir(parents=True, exist_ok=True)
    disk = shutil.disk_usage(download_path)
    reserved = get_reserved_space()
    return max(0, disk.free - reserved)


def get_user_available_space_for_pack(user_id: int) -> int:
    """Get user available space for pack (considers quota, disk, and reserved)

    Returns minimum of:
    - User remaining quota
    - Server available space (minus reserved)
    """
    from app.db import fetch_one

    # Get user quota
    user = fetch_one("SELECT quota FROM users WHERE id = ?", [user_id])
    user_quota = user["quota"] if user and user.get("quota") else 100 * 1024 * 1024 * 1024

    # Calculate user's current usage
    user_dir = Path(settings.download_dir) / str(user_id)
    used_space = 0
    if user_dir.exists():
        for file_path in user_dir.rglob("*"):
            if file_path.is_file():
                try:
                    used_space += file_path.stat().st_size
                except Exception:
                    pass

    user_remaining = max(0, user_quota - used_space)
    server_available = get_server_available_space()

    return min(user_remaining, server_available)
```

#### 2.2 Modify File: `backend/app/routers/files.py`

Add pack endpoints after existing endpoints:

```python
# Add imports at top
import asyncio
from app.services.pack import (
    PackTaskManager, calculate_folder_size,
    get_user_available_space_for_pack, get_server_available_space
)

# Add schemas
class PackRequest(BaseModel):
    """Create pack task request"""
    folder_path: str


class PackTaskResponse(BaseModel):
    """Pack task response"""
    id: int
    owner_id: int
    folder_path: str
    folder_size: int
    reserved_space: int
    output_path: str | None
    output_size: int | None
    status: str
    progress: int
    error_message: str | None
    created_at: str
    updated_at: str


# Add endpoints

@router.post("/pack", status_code=status.HTTP_201_CREATED)
async def create_pack_task(
    payload: PackRequest,
    user: dict = Depends(require_user)
) -> dict:
    """Create a new folder pack task

    Validates:
    - Folder exists and belongs to user
    - User has enough space (quota + server)

    Reserves space and starts async packing in background.
    """
    from app.db import execute, fetch_one
    from app.aria2.sync import broadcast_update

    user_dir = _get_user_dir(user["id"])
    target = _validate_path(user_dir, payload.folder_path)

    if not target.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Folder not found"
        )

    if not target.is_dir():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path is not a folder"
        )

    # Calculate folder size
    folder_size = calculate_folder_size(target)
    if folder_size == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Folder is empty"
        )

    # Reserve space = folder size (compressed output will be smaller but we reserve full)
    reserved_space = folder_size

    # Check available space
    available = get_user_available_space_for_pack(user["id"])
    if reserved_space > available:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Not enough space. Required: {reserved_space / 1024 / 1024 / 1024:.2f} GB, Available: {available / 1024 / 1024 / 1024:.2f} GB"
        )

    # Create task record
    from app.db import utc_now
    task_id = execute(
        """
        INSERT INTO pack_tasks
        (owner_id, folder_path, folder_size, reserved_space, status, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [user["id"], payload.folder_path, folder_size, reserved_space, "pending", utc_now(), utc_now()]
    )

    # Start async packing
    asyncio.create_task(PackTaskManager.start_pack(task_id, user["id"], payload.folder_path))

    return fetch_one("SELECT * FROM pack_tasks WHERE id = ?", [task_id])


@router.get("/pack")
def list_pack_tasks(user: dict = Depends(require_user)) -> list[dict]:
    """List user's pack tasks (ordered by created_at desc)"""
    from app.db import fetch_all
    return fetch_all(
        """SELECT * FROM pack_tasks
           WHERE owner_id = ?
           ORDER BY created_at DESC""",
        [user["id"]]
    )


@router.get("/pack/{task_id}")
def get_pack_task(task_id: int, user: dict = Depends(require_user)) -> dict:
    """Get pack task details"""
    from app.db import fetch_one
    task = fetch_one(
        "SELECT * FROM pack_tasks WHERE id = ? AND owner_id = ?",
        [task_id, user["id"]]
    )
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task


@router.delete("/pack/{task_id}")
async def cancel_pack_task(task_id: int, user: dict = Depends(require_user)) -> dict:
    """Cancel a pack task

    - Cancels running process
    - Releases reserved space
    - Deletes temporary output file
    """
    from app.db import fetch_one, execute

    task = fetch_one(
        "SELECT * FROM pack_tasks WHERE id = ? AND owner_id = ?",
        [task_id, user["id"]]
    )
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    if task["status"] in ("done", "cancelled", "failed"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Task already completed or cancelled"
        )

    # Cancel running process
    await PackTaskManager.cancel_pack(task_id)

    # Update status (in case process wasn't running)
    execute(
        "UPDATE pack_tasks SET status = ?, reserved_space = 0, updated_at = ? WHERE id = ?",
        ["cancelled", utc_now(), task_id]
    )

    return {"ok": True, "message": "Task cancelled"}


@router.get("/pack/{task_id}/download")
def download_pack_result(task_id: int, user: dict = Depends(require_user)) -> FileResponse:
    """Download completed pack file"""
    from app.db import fetch_one

    task = fetch_one(
        "SELECT * FROM pack_tasks WHERE id = ? AND owner_id = ?",
        [task_id, user["id"]]
    )
    if not task:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")

    if task["status"] != "done":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Pack task not completed"
        )

    output_path = task.get("output_path")
    if not output_path or not Path(output_path).exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pack file not found")

    return FileResponse(
        path=output_path,
        filename=Path(output_path).name,
        media_type="application/octet-stream"
    )


@router.get("/pack/available-space")
def get_pack_available_space(user: dict = Depends(require_user)) -> dict:
    """Get available space for pack operations"""
    available = get_user_available_space_for_pack(user["id"])
    server_available = get_server_available_space()

    return {
        "user_available": available,
        "server_available": server_available,
    }
```

#### 2.3 Modify File: `backend/app/routers/config.py`

Add config getters and admin endpoints:

```python
# Add after existing get_hidden_file_extensions()

def get_pack_format() -> str:
    """Get pack format (zip or 7z), default zip"""
    val = get_config_value("pack_format")
    return val if val in ("zip", "7z") else "zip"


def get_pack_compression_level() -> int:
    """Get pack compression level (1-9), default 5"""
    val = get_config_value("pack_compression_level")
    try:
        level = int(val) if val else 5
        return max(1, min(9, level))
    except ValueError:
        return 5


# Modify ConfigUpdate class - add fields:
class ConfigUpdate(BaseModel):
    """Configuration update request"""
    max_task_size: int | None = None
    min_free_disk: int | None = None
    aria2_rpc_url: str | None = None
    aria2_rpc_secret: str | None = None
    hidden_file_extensions: list[str] | None = None
    pack_format: str | None = None  # NEW
    pack_compression_level: int | None = None  # NEW


# Modify get_config() - add to return dict:
def get_config(admin: dict = Depends(require_admin)) -> dict:
    # ... existing code ...
    return {
        # ... existing fields ...
        "pack_format": get_pack_format(),
        "pack_compression_level": get_pack_compression_level(),
    }


# Modify update_config() - add handlers:
def update_config(payload: ConfigUpdate, admin: dict = Depends(require_admin)) -> dict:
    # ... existing code ...

    if payload.pack_format is not None:
        if payload.pack_format in ("zip", "7z"):
            set_config_value("pack_format", payload.pack_format)

    if payload.pack_compression_level is not None:
        level = max(1, min(9, payload.pack_compression_level))
        set_config_value("pack_compression_level", str(level))

    # ... return updated config ...
```

### 3. Frontend Code Changes

#### 3.1 Modify File: `frontend/types.ts`

Add new types:

```typescript
export type PackTask = {
  id: number;
  owner_id: number;
  folder_path: string;
  folder_size: number;
  reserved_space: number;
  output_path: string | null;
  output_size: number | null;
  status: "pending" | "packing" | "done" | "failed" | "cancelled";
  progress: number;
  error_message: string | null;
  created_at: string;
  updated_at: string;
};

export type PackAvailableSpace = {
  user_available: number;
  server_available: number;
};

// Update SystemConfig
export type SystemConfig = {
  max_task_size: number;
  min_free_disk: number;
  aria2_rpc_url: string;
  aria2_rpc_secret: string;
  hidden_file_extensions: string[];
  pack_format: "zip" | "7z";
  pack_compression_level: number;
};
```

#### 3.2 Modify File: `frontend/lib/api.ts`

Add pack API methods:

```typescript
import type {
  // ... existing imports ...
  PackTask,
  PackAvailableSpace,
} from "@/types";

export const api = {
  // ... existing methods ...

  // Pack Tasks
  createPackTask: (folderPath: string) =>
    request<PackTask>("/api/files/pack", {
      method: "POST",
      body: JSON.stringify({ folder_path: folderPath }),
    }),

  listPackTasks: () => request<PackTask[]>("/api/files/pack"),

  getPackTask: (id: number) => request<PackTask>(`/api/files/pack/${id}`),

  cancelPackTask: (id: number) =>
    request<{ ok: boolean; message: string }>(`/api/files/pack/${id}`, {
      method: "DELETE",
    }),

  downloadPackResult: (id: number) => {
    const base = getApiBase();
    return `${base}/api/files/pack/${id}/download`;
  },

  getPackAvailableSpace: () =>
    request<PackAvailableSpace>("/api/files/pack/available-space"),
};
```

#### 3.3 New File: `frontend/components/PackConfirmModal.tsx`

```tsx
"use client";

import { useState } from "react";
import { formatBytes } from "@/lib/utils";

interface PackConfirmModalProps {
  folderName: string;
  folderSize: number;
  availableSpace: number;
  onConfirm: () => void;
  onCancel: () => void;
  loading?: boolean;
}

export default function PackConfirmModal({
  folderName,
  folderSize,
  availableSpace,
  onConfirm,
  onCancel,
  loading = false,
}: PackConfirmModalProps) {
  const canPack = folderSize <= availableSpace;

  return (
    <div
      style={{
        position: "fixed",
        top: 0,
        left: 0,
        right: 0,
        bottom: 0,
        background: "rgba(0,0,0,0.5)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 1000,
      }}
      onClick={onCancel}
    >
      <div
        className="card"
        style={{
          width: 480,
          maxWidth: "90vw",
          padding: 24,
        }}
        onClick={(e) => e.stopPropagation()}
      >
        <h2 style={{ marginBottom: 16 }}>Confirm Folder Pack</h2>

        <div style={{ marginBottom: 16 }}>
          <p style={{ marginBottom: 8 }}>
            <strong>Folder:</strong> {folderName}
          </p>
          <p style={{ marginBottom: 8 }}>
            <strong>Size:</strong> {formatBytes(folderSize)}
          </p>
          <p style={{ marginBottom: 8 }}>
            <strong>Available Space:</strong> {formatBytes(availableSpace)}
          </p>
        </div>

        <div
          style={{
            padding: 12,
            background: "rgba(255, 149, 0, 0.1)",
            border: "1px solid rgba(255, 149, 0, 0.3)",
            borderRadius: 8,
            marginBottom: 16,
          }}
        >
          <p style={{ margin: 0, fontSize: 13, color: "#ff9500" }}>
            <strong>Warning:</strong>
          </p>
          <ul style={{ margin: "8px 0 0 16px", fontSize: 13, color: "#ff9500" }}>
            <li>
              Packing will freeze {formatBytes(folderSize)} of space during
              operation
            </li>
            <li>
              Source folder will be DELETED after successful packing
            </li>
            <li>Only the compressed archive will remain</li>
          </ul>
        </div>

        {!canPack && (
          <div
            style={{
              padding: 12,
              background: "rgba(255, 59, 48, 0.1)",
              border: "1px solid rgba(255, 59, 48, 0.3)",
              borderRadius: 8,
              marginBottom: 16,
            }}
          >
            <p style={{ margin: 0, fontSize: 13, color: "#ff3b30" }}>
              Not enough space available for this operation.
            </p>
          </div>
        )}

        <div style={{ display: "flex", gap: 12, justifyContent: "flex-end" }}>
          <button
            className="button secondary"
            onClick={onCancel}
            disabled={loading}
          >
            Cancel
          </button>
          <button
            className="button"
            onClick={onConfirm}
            disabled={loading || !canPack}
          >
            {loading ? "Creating..." : "Confirm Pack"}
          </button>
        </div>
      </div>
    </div>
  );
}
```

#### 3.4 New File: `frontend/components/PackTaskCard.tsx`

```tsx
"use client";

import { useState, useEffect, useRef } from "react";
import { api } from "@/lib/api";
import { formatBytes } from "@/lib/utils";
import type { PackTask } from "@/types";

export default function PackTaskCard() {
  const [tasks, setTasks] = useState<PackTask[]>([]);
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(false);
  const cardRef = useRef<HTMLDivElement>(null);

  // Load tasks
  const loadTasks = async () => {
    try {
      const data = await api.listPackTasks();
      setTasks(data);
    } catch (err) {
      console.error("Failed to load pack tasks:", err);
    }
  };

  // Poll for updates when there are active tasks
  useEffect(() => {
    loadTasks();

    const hasActiveTasks = tasks.some(
      (t) => t.status === "pending" || t.status === "packing"
    );

    if (hasActiveTasks) {
      const interval = setInterval(loadTasks, 2000);
      return () => clearInterval(interval);
    }
  }, [tasks.length]);

  // Click outside to close
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (cardRef.current && !cardRef.current.contains(event.target as Node)) {
        setExpanded(false);
      }
    };

    if (expanded) {
      document.addEventListener("mousedown", handleClickOutside);
      return () => document.removeEventListener("mousedown", handleClickOutside);
    }
  }, [expanded]);

  const activeTasks = tasks.filter(
    (t) => t.status === "pending" || t.status === "packing"
  );

  const handleCancel = async (taskId: number) => {
    if (!confirm("Cancel this pack task?")) return;
    try {
      await api.cancelPackTask(taskId);
      loadTasks();
    } catch (err) {
      alert(`Failed to cancel: ${(err as Error).message}`);
    }
  };

  const getStatusColor = (status: string) => {
    switch (status) {
      case "packing":
        return "#0071e3";
      case "done":
        return "#34c759";
      case "failed":
        return "#ff3b30";
      case "cancelled":
        return "#8e8e93";
      default:
        return "#ff9500";
    }
  };

  const getStatusText = (status: string) => {
    switch (status) {
      case "pending":
        return "Pending";
      case "packing":
        return "Packing";
      case "done":
        return "Done";
      case "failed":
        return "Failed";
      case "cancelled":
        return "Cancelled";
      default:
        return status;
    }
  };

  if (tasks.length === 0) return null;

  return (
    <div ref={cardRef} style={{ position: "relative" }}>
      <button
        className="button secondary"
        style={{
          padding: "8px 16px",
          display: "flex",
          alignItems: "center",
          gap: 8,
        }}
        onClick={() => setExpanded(!expanded)}
      >
        <span>Pack Tasks</span>
        {activeTasks.length > 0 && (
          <span
            style={{
              background: "#0071e3",
              color: "white",
              borderRadius: 10,
              padding: "2px 8px",
              fontSize: 12,
              fontWeight: 600,
            }}
          >
            {activeTasks.length}
          </span>
        )}
      </button>

      {expanded && (
        <div
          className="card"
          style={{
            position: "absolute",
            top: "100%",
            right: 0,
            marginTop: 8,
            width: 360,
            maxHeight: 400,
            overflowY: "auto",
            zIndex: 100,
            padding: 0,
          }}
        >
          {tasks.map((task) => (
            <div
              key={task.id}
              style={{
                padding: 16,
                borderBottom: "1px solid rgba(0,0,0,0.05)",
              }}
            >
              <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
                <span style={{ fontWeight: 500, fontSize: 14 }}>
                  {task.folder_path}
                </span>
                <span
                  style={{
                    fontSize: 12,
                    color: getStatusColor(task.status),
                    fontWeight: 600,
                  }}
                >
                  {getStatusText(task.status)}
                </span>
              </div>

              {(task.status === "pending" || task.status === "packing") && (
                <>
                  <div
                    style={{
                      height: 4,
                      background: "rgba(0,0,0,0.05)",
                      borderRadius: 2,
                      marginBottom: 8,
                      overflow: "hidden",
                    }}
                  >
                    <div
                      style={{
                        height: "100%",
                        width: `${task.progress}%`,
                        background: "#0071e3",
                        transition: "width 0.3s ease",
                      }}
                    />
                  </div>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                    <span className="muted" style={{ fontSize: 12 }}>
                      {task.progress}% - Reserved: {formatBytes(task.reserved_space)}
                    </span>
                    <button
                      className="button secondary danger"
                      style={{ padding: "4px 12px", fontSize: 12 }}
                      onClick={() => handleCancel(task.id)}
                    >
                      Cancel
                    </button>
                  </div>
                </>
              )}

              {task.status === "done" && (
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                  <span className="muted" style={{ fontSize: 12 }}>
                    Output: {formatBytes(task.output_size || 0)}
                  </span>
                  <a
                    className="button secondary"
                    style={{ padding: "4px 12px", fontSize: 12 }}
                    href={api.downloadPackResult(task.id)}
                    download
                  >
                    Download
                  </a>
                </div>
              )}

              {task.status === "failed" && task.error_message && (
                <p style={{ margin: 0, fontSize: 12, color: "#ff3b30" }}>
                  {task.error_message}
                </p>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
```

#### 3.5 Modify File: `frontend/app/files/page.tsx`

Add pack functionality:

```tsx
// Add imports
import { useState, useEffect } from "react";
import PackConfirmModal from "@/components/PackConfirmModal";
import PackTaskCard from "@/components/PackTaskCard";
import type { PackAvailableSpace } from "@/types";

// Inside component, add state:
const [packingFolder, setPackingFolder] = useState<FileInfo | null>(null);
const [packAvailableSpace, setPackAvailableSpace] = useState<number>(0);
const [packLoading, setPackLoading] = useState(false);
const [packTasksKey, setPackTasksKey] = useState(0); // For refresh

// Add function to start pack:
const handleStartPack = async (file: FileInfo) => {
  try {
    const space = await api.getPackAvailableSpace();
    setPackAvailableSpace(space.user_available);
    setPackingFolder(file);
  } catch (err) {
    alert(`Failed to check space: ${(err as Error).message}`);
  }
};

const handleConfirmPack = async () => {
  if (!packingFolder) return;

  setPackLoading(true);
  try {
    await api.createPackTask(packingFolder.path);
    setPackingFolder(null);
    setPackTasksKey((k) => k + 1); // Refresh pack task card
    // Optionally reload files
    loadFiles(currentPath);
  } catch (err) {
    alert(`Failed to create pack task: ${(err as Error).message}`);
  } finally {
    setPackLoading(false);
  }
};

// In JSX, add PackTaskCard in header area:
<div className="space-between" style={{ marginBottom: 32 }}>
  <div>
    <h1 style={{ fontSize: "28px" }}>Files</h1>
    <p className="muted">Manage your downloaded files</p>
  </div>
  <PackTaskCard key={packTasksKey} />
</div>

// Add Pack button in file row actions (for folders only):
{file.is_dir && (
  <button
    className="button secondary"
    style={{ padding: "6px 12px", fontSize: "13px" }}
    onClick={() => handleStartPack(file)}
  >
    Pack
  </button>
)}

// Add modal at end of component:
{packingFolder && (
  <PackConfirmModal
    folderName={packingFolder.name}
    folderSize={packingFolder.size || 0}
    availableSpace={packAvailableSpace}
    onConfirm={handleConfirmPack}
    onCancel={() => setPackingFolder(null)}
    loading={packLoading}
  />
)}
```

**Note**: Need to calculate folder size on demand. Modify `handleStartPack`:

```tsx
const handleStartPack = async (file: FileInfo) => {
  try {
    // Get folder size by listing all files
    const calculateSize = async (path: string): Promise<number> => {
      const response = await api.listFiles(path);
      let total = 0;
      for (const f of response.files) {
        if (f.is_dir) {
          total += await calculateSize(f.path);
        } else {
          total += f.size;
        }
      }
      return total;
    };

    const folderSize = await calculateSize(file.path);
    const space = await api.getPackAvailableSpace();

    setPackAvailableSpace(space.user_available);
    setPackingFolder({ ...file, size: folderSize });
  } catch (err) {
    alert(`Failed to check folder: ${(err as Error).message}`);
  }
};
```

#### 3.6 Modify File: `frontend/app/settings/page.tsx`

Add pack settings section:

```tsx
// Add state variables:
const [packFormat, setPackFormat] = useState<"zip" | "7z">("zip");
const [packCompressionLevel, setPackCompressionLevel] = useState(5);

// In loadConfig(), add:
setPackFormat(cfg.pack_format || "zip");
setPackCompressionLevel(cfg.pack_compression_level || 5);

// In saveConfig(), add to updateConfig payload:
await api.updateConfig({
  // ... existing fields ...
  pack_format: packFormat,
  pack_compression_level: packCompressionLevel,
});

// Add UI section after "File Management Config" section:
<h2 style={{ marginBottom: 24, marginTop: 32 }}>Pack Settings</h2>

<div style={{ marginBottom: 24 }}>
  <label style={{ display: "block", marginBottom: 8, fontWeight: 600 }}>
    Pack Format
  </label>
  <p className="muted" style={{ fontSize: 13, marginBottom: 12 }}>
    Choose archive format for folder packing.
  </p>
  <div style={{ display: "flex", gap: 16 }}>
    <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
      <input
        type="radio"
        name="packFormat"
        value="zip"
        checked={packFormat === "zip"}
        onChange={() => setPackFormat("zip")}
      />
      <span>ZIP</span>
    </label>
    <label style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
      <input
        type="radio"
        name="packFormat"
        value="7z"
        checked={packFormat === "7z"}
        onChange={() => setPackFormat("7z")}
      />
      <span>7Z</span>
    </label>
  </div>
</div>

<div style={{ marginBottom: 32 }}>
  <label style={{ display: "block", marginBottom: 8, fontWeight: 600 }}>
    Compression Level: {packCompressionLevel}
  </label>
  <p className="muted" style={{ fontSize: 13, marginBottom: 12 }}>
    1 = fastest/largest, 9 = slowest/smallest
  </p>
  <input
    type="range"
    min="1"
    max="9"
    value={packCompressionLevel}
    onChange={(e) => setPackCompressionLevel(parseInt(e.target.value))}
    style={{ width: "100%", maxWidth: 300 }}
  />
</div>
```

---

## Implementation Sequence

### Phase 1: Database and Backend Core (Independently Deployable)

1. **Modify `backend/app/db.py`**
   - Add `pack_tasks` table creation in `init_db()`
   - Add default config values for `pack_format` and `pack_compression_level`

2. **Create `backend/app/services/pack.py`**
   - Implement `PackTaskManager` class
   - Implement space calculation functions

3. **Modify `backend/app/routers/config.py`**
   - Add `get_pack_format()` and `get_pack_compression_level()` functions
   - Add new fields to `ConfigUpdate` schema
   - Update `get_config()` and `update_config()` endpoints

**Validation**: Backend starts without errors, config endpoints return new fields.

### Phase 2: Pack API Endpoints (Independently Deployable)

4. **Modify `backend/app/routers/files.py`**
   - Add pack-related imports and schemas
   - Add `POST /api/files/pack` endpoint
   - Add `GET /api/files/pack` endpoint
   - Add `GET /api/files/pack/{id}` endpoint
   - Add `DELETE /api/files/pack/{id}` endpoint
   - Add `GET /api/files/pack/{id}/download` endpoint
   - Add `GET /api/files/pack/available-space` endpoint

**Validation**: API endpoints respond correctly, pack task can be created and tracked.

### Phase 3: Frontend Types and API (Independently Deployable)

5. **Modify `frontend/types.ts`**
   - Add `PackTask` type
   - Add `PackAvailableSpace` type
   - Update `SystemConfig` type

6. **Modify `frontend/lib/api.ts`**
   - Add all pack-related API methods

**Validation**: TypeScript compiles without errors.

### Phase 4: Frontend Components (Independently Deployable)

7. **Create `frontend/components/PackConfirmModal.tsx`**

8. **Create `frontend/components/PackTaskCard.tsx`**

**Validation**: Components render without errors.

### Phase 5: Frontend Integration (Complete Feature)

9. **Modify `frontend/app/files/page.tsx`**
   - Add pack functionality to file list
   - Integrate modal and task card

10. **Modify `frontend/app/settings/page.tsx`**
    - Add pack format and compression settings

**Validation**: Full feature works end-to-end.

---

## Validation Plan

### Unit Tests

1. **Pack Service Tests** (`backend/tests/test_pack.py`)
   - `test_calculate_folder_size`: Verify correct size calculation
   - `test_get_reserved_space`: Verify reserved space aggregation
   - `test_get_server_available_space`: Verify space minus reserved
   - `test_pack_format_config`: Verify config get/set
   - `test_compression_level_bounds`: Verify 1-9 clamping

2. **Pack API Tests** (`backend/tests/test_pack_api.py`)
   - `test_create_pack_task_success`: Happy path
   - `test_create_pack_task_not_folder`: Error on file
   - `test_create_pack_task_not_enough_space`: Space check
   - `test_cancel_pack_task`: Cancel running task
   - `test_download_pack_result`: Download after completion

### Integration Tests

1. **End-to-End Pack Flow**
   - Create folder with test files
   - Initiate pack task
   - Verify progress updates
   - Verify source folder deleted
   - Download and verify archive contents

2. **Space Reservation Test**
   - Create multiple pack tasks
   - Verify reserved space accumulates
   - Verify new task rejected when space insufficient

3. **Cancel Test**
   - Start pack task
   - Cancel mid-pack
   - Verify no partial file remains
   - Verify reserved space released

### Business Logic Verification

1. **Folder disappears after pack**: Source folder must not exist after successful pack
2. **Archive contains correct files**: Unpack and compare with original
3. **Space accounting**: Reserved space = 0 after task completes/fails/cancels
4. **User isolation**: Users cannot see or access other users' pack tasks
5. **Admin settings take effect**: Changing format/level affects new pack tasks

---

## File Summary

| File | Action | Description |
|------|--------|-------------|
| `backend/app/db.py` | Modify | Add pack_tasks table, config defaults |
| `backend/app/services/pack.py` | Create | Pack task manager, space calculations |
| `backend/app/routers/config.py` | Modify | Add pack config getters, update endpoints |
| `backend/app/routers/files.py` | Modify | Add pack CRUD endpoints |
| `frontend/types.ts` | Modify | Add PackTask, PackAvailableSpace types |
| `frontend/lib/api.ts` | Modify | Add pack API methods |
| `frontend/components/PackConfirmModal.tsx` | Create | Confirmation dialog |
| `frontend/components/PackTaskCard.tsx` | Create | Task list dropdown |
| `frontend/app/files/page.tsx` | Modify | Integrate pack UI |
| `frontend/app/settings/page.tsx` | Modify | Add pack admin settings |

---

## Dependencies

- **7-zip CLI**: Must be installed on server (`7z` command available)
- No new Python packages required (uses asyncio subprocess)
- No new frontend packages required

---

## Notes

1. **7-zip Installation**:
   - macOS: `brew install p7zip`
   - Ubuntu: `apt install p7zip-full`
   - The `7z` command must be in PATH

2. **Progress Parsing**: 7-zip outputs progress like ` 45%` to stdout with `-bsp1` flag. Regex `(\d+)%` captures this.

3. **Space Reservation Strategy**: We reserve the full folder size (worst case) even though compressed output will be smaller. This ensures we never over-allocate.

4. **Source Deletion Timing**: Source folder is deleted only after 7-zip exits with code 0, ensuring atomic completion.
