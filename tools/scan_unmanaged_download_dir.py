#!/usr/bin/env python3
"""Scan files under Aria2Deck download_dir that are outside managed layout.

Managed layout (not reported as unmanaged):
  <download_dir>/store/**
  <download_dir>/downloading/<digits>/
  <download_dir>/downloading/pack_<digits>/
  <download_dir>/.aria2deck-write-test

Everything else under download_dir is treated as unmanaged and printed as a
tree with human-readable sizes at every level.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

TASK_DIR_RE = re.compile(r"^\d+$")
PACK_DIR_RE = re.compile(r"^pack_\d+$")
MANAGED_ROOT_DIRS = frozenset({"store", "downloading"})
KNOWN_ROOT_FILES = frozenset({".aria2deck-write-test"})


@dataclass
class Node:
    name: str
    path: Path
    is_dir: bool
    size: int = 0
    children: list["Node"] = field(default_factory=list)


def human_size(num: int) -> str:
    if num < 0:
        num = 0
    units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    value = float(num)
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)}B"
            return f"{value:.1f}{unit}"
        value /= 1024.0
    return f"{num}B"


def resolve_download_dir(cli_value: str | None) -> Path:
    raw = (
        cli_value
        or os.environ.get("ARIA2C_DOWNLOAD_DIR")
        or os.environ.get("DOWNLOAD_DIR")
        or ""
    ).strip()
    if not raw:
        raise SystemExit(
            "未指定下载目录。请传 --download-dir，或设置 ARIA2C_DOWNLOAD_DIR。"
        )
    path = Path(raw).expanduser().resolve()
    if not path.exists():
        raise SystemExit(f"下载目录不存在: {path}")
    if not path.is_dir():
        raise SystemExit(f"下载目录不是文件夹: {path}")
    return path


def is_managed_downloading_entry(name: str) -> bool:
    return bool(TASK_DIR_RE.fullmatch(name) or PACK_DIR_RE.fullmatch(name))


def is_managed_root_entry(name: str, is_dir: bool) -> bool:
    if is_dir and name in MANAGED_ROOT_DIRS:
        return True
    if not is_dir and name in KNOWN_ROOT_FILES:
        return True
    return False


def entry_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def build_unmanaged_tree(root: Path) -> Node:
    root_node = Node(name=str(root), path=root, is_dir=True)

    def walk_unmanaged(directory: Path, parent: Node, *, under_downloading: bool) -> int:
        total = 0
        try:
            entries = sorted(
                directory.iterdir(),
                key=lambda p: (not p.is_dir(), p.name.lower()),
            )
        except OSError as exc:
            print(f"警告: 无法读取目录 {directory}: {exc}", file=sys.stderr)
            return 0

        for entry in entries:
            try:
                is_dir = entry.is_dir() and not entry.is_symlink()
                is_symlink = entry.is_symlink()
            except OSError as exc:
                print(f"警告: 无法访问 {entry}: {exc}", file=sys.stderr)
                continue

            # Root-level managed zones.
            if directory == root and is_managed_root_entry(entry.name, is_dir):
                if entry.name == "downloading" and is_dir:
                    # Enter downloading, but only keep unmanaged children.
                    child = Node(name=entry.name, path=entry, is_dir=True)
                    child_size = walk_unmanaged(
                        entry, child, under_downloading=True
                    )
                    if child.children:
                        child.size = child_size
                        parent.children.append(child)
                        total += child_size
                # store/ and known probe file are fully skipped.
                continue

            # Inside downloading/: numeric task dirs and pack_* are managed.
            if under_downloading and is_managed_downloading_entry(entry.name):
                continue

            if is_dir:
                child = Node(name=entry.name, path=entry, is_dir=True)
                child_size = walk_unmanaged(
                    entry, child, under_downloading=False
                )
                child.size = child_size
                parent.children.append(child)
                total += child_size
            else:
                size = entry_size(entry)
                # Symlinks: report as files with target size 0 if broken.
                if is_symlink:
                    size = entry_size(entry)
                child = Node(
                    name=entry.name,
                    path=entry,
                    is_dir=False,
                    size=size,
                )
                parent.children.append(child)
                total += size

        return total

    root_node.size = walk_unmanaged(root, root_node, under_downloading=False)
    return root_node


def print_tree(node: Node, *, show_paths: bool = False) -> None:
    if not node.children:
        print(f"{node.name}  [无未管理文件]")
        print(f"└── (empty)  0B")
        return

    print(f"{node.name}  [{human_size(node.size)} unmanaged]")

    def emit(current: Node, prefix: str, is_last: bool) -> None:
        connector = "└── " if is_last else "├── "
        kind = "dir" if current.is_dir else "file"
        label = current.name + ("/" if current.is_dir else "")
        size_label = human_size(current.size)
        line = f"{prefix}{connector}{label}  [{kind}]  {size_label}"
        if show_paths:
            line += f"  ({current.path})"
        print(line)

        if not current.children:
            return
        extension = "    " if is_last else "│   "
        for index, child in enumerate(current.children):
            emit(child, prefix + extension, index == len(current.children) - 1)

    for index, child in enumerate(node.children):
        emit(child, "", index == len(node.children) - 1)


def count_nodes(node: Node) -> tuple[int, int]:
    files = 0
    dirs = 0
    for child in node.children:
        if child.is_dir:
            dirs += 1
            d, f = count_nodes(child)
            dirs += d
            files += f
        else:
            files += 1
    return dirs, files


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "扫描 Aria2Deck 下载目录中、不在系统管理布局内的文件，"
            "以 tree 形式展示并标注每一级大小。"
        )
    )
    parser.add_argument(
        "--download-dir",
        help="下载目录路径；默认读取环境变量 ARIA2C_DOWNLOAD_DIR",
    )
    parser.add_argument(
        "--show-paths",
        action="store_true",
        help="在树节点后附加绝对路径",
    )
    parser.add_argument(
        "--json-summary",
        action="store_true",
        help="额外输出一行 JSON 汇总（便于脚本消费）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = resolve_download_dir(args.download_dir)
    tree = build_unmanaged_tree(root)
    print_tree(tree, show_paths=args.show_paths)

    dirs, files = count_nodes(tree)
    print()
    print(
        f"汇总: unmanaged_dirs={dirs} unmanaged_files={files} "
        f"total_size={human_size(tree.size)} ({tree.size} bytes)"
    )
    print(
        "说明: store/** 与 downloading/<id|pack_id>/ 视为系统管理布局，"
        "不在本扫描结果中列出。"
    )

    if args.json_summary:
        import json

        print(
            json.dumps(
                {
                    "download_dir": str(root),
                    "unmanaged_dirs": dirs,
                    "unmanaged_files": files,
                    "total_bytes": tree.size,
                },
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
