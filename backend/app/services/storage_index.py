from __future__ import annotations

from pathlib import Path
from typing import Any


def _mtime_ms(path: Path) -> int | None:
    try:
        return int(path.stat().st_mtime * 1000)
    except FileNotFoundError:
        return None


def _size_bytes(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return 0


def _relative_path(root: Path, path: Path) -> str:
    if path == root:
        return "."
    return path.relative_to(root).as_posix()


def _parent_path(root: Path, path: Path) -> str:
    if path == root:
        return ""

    relative_parent = path.relative_to(root).parent
    if relative_parent == Path("."):
        return ""
    return relative_parent.as_posix()


def _sort_key(parent_path: str, name: str, is_dir: bool) -> str:
    kind = "0" if is_dir else "1"
    return f"{parent_path}\0{kind}\0{name.lower()}"


def build_entry_templates(root: Path) -> list[dict[str, Any]]:
    root = root.resolve(strict=False)
    paths = [root]
    if root.is_dir():
        paths.extend(root.rglob("*"))

    entries: list[dict[str, Any]] = []
    for path in paths:
        is_dir = path.is_dir()
        parent_path = _parent_path(root, path)
        name = path.name or "."
        entries.append(
            {
                "relative_path": _relative_path(root, path),
                "parent_path": parent_path,
                "name": name,
                "size_bytes": _size_bytes(path),
                "is_dir": 1 if is_dir else 0,
                "mtime_ms": _mtime_ms(path),
                "sort_key": _sort_key(parent_path, name, is_dir),
            }
        )

    return sorted(
        entries,
        key=lambda entry: (
            entry["relative_path"] != ".",
            entry["parent_path"],
            entry["sort_key"],
            entry["relative_path"],
        ),
    )


def build_entries(stored_file_id: int, root: Path) -> list[dict[str, Any]]:
    return [
        {"stored_file_id": stored_file_id, **entry}
        for entry in build_entry_templates(root)
    ]
