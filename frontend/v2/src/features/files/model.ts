/**
 * F1 文件功能模型：归档参考 视图形状、排序规则、搜索命中映射与逐页定位。
 */
import type { FileItemResponse, FileSearchHitResponse } from "@/shared/api/types";

export type SortField = "name" | "size" | "created_at";
export type SortOrder = "asc" | "desc";

/** 归档参考 FileInfo 视图（数字 id，无 content_hash）。 */
export interface FileView {
  id: number;
  name: string;
  size: number;
  isDirectory: boolean;
  createdAtMs: number;
}

export interface BrowseItemView {
  name: string;
  size: number;
  isDirectory: boolean;
}

export interface SearchHitView {
  userFileId: number;
  name: string;
  /** 相对路径（含文件名）。 */
  path: string;
  size: number;
  isDirectory: boolean;
  /** 所在目录内的相对路径；null = 根列表项（C31：V2 无 root_index，由 path 推导）。 */
  entryPath: string | null;
}

export function toFileView(item: FileItemResponse): FileView {
  return {
    id: item.id,
    name: item.display_name,
    size: item.size,
    isDirectory: item.kind === "directory",
    createdAtMs: item.created_at_ms,
  };
}

export function toBrowseItemView(item: { name: string; size: number; kind: string }): BrowseItemView {
  return { name: item.name, size: item.size, isDirectory: item.kind === "directory" };
}

export function toSearchHitView(hit: FileSearchHitResponse): SearchHitView {
  const segments = hit.path.split("/").filter(Boolean);
  return {
    userFileId: hit.user_file_id,
    name: hit.display_name,
    path: hit.path,
    size: hit.size,
    isDirectory: hit.kind === "directory",
    entryPath: segments.length > 1 ? segments.join("/") : null,
  };
}

/** 归档参考 排序：文件夹永远在前，然后按字段与方向。 */
export function sortFiles(files: readonly FileView[], field: SortField, order: SortOrder): FileView[] {
  const factor = order === "asc" ? 1 : -1;
  return [...files].sort((a, b) => {
    if (a.isDirectory && !b.isDirectory) return -1;
    if (!a.isDirectory && b.isDirectory) return 1;
    let cmp = 0;
    if (field === "name") cmp = a.name.localeCompare(b.name);
    else if (field === "size") cmp = a.size - b.size;
    else cmp = a.createdAtMs - b.createdAtMs;
    return cmp * factor;
  });
}

/** 目录内排序：目录优先；created_at 在目录项不可用，回退名称（归档参考 原逻辑）。 */
export function sortBrowseItems(
  items: readonly BrowseItemView[],
  field: SortField,
  order: SortOrder,
): BrowseItemView[] {
  const factor = order === "asc" ? 1 : -1;
  const effective = field === "created_at" ? "name" : field;
  return [...items].sort((a, b) => {
    if (a.isDirectory && !b.isDirectory) return -1;
    if (!a.isDirectory && b.isDirectory) return 1;
    const cmp = effective === "name" ? a.name.localeCompare(b.name) : a.size - b.size;
    return cmp * factor;
  });
}

/** 归档参考 formatDate：`y/m/d hh:mm`。 */
export function formatFileDate(ms: number): string {
  const date = new Date(ms);
  const y = date.getFullYear();
  const m = date.getMonth() + 1;
  const d = date.getDate();
  const hh = String(date.getHours()).padStart(2, "0");
  const mm = String(date.getMinutes()).padStart(2, "0");
  return `${y}/${m}/${d} ${hh}:${mm}`;
}

/**
 * C31：V2 搜索命中没有 root_index，根列表定位改为按 id 逐页扫描。
 * 返回目标所在页（1 起）；未找到返回 null。调用方负责设页码并高亮。
 */
export async function locateFilePage(
  fetchPage: (page: number) => Promise<{ items: { id: number }[]; total: number }>,
  targetId: number,
  pageSize: number,
): Promise<number | null> {
  const first = await fetchPage(1);
  if (first.items.some((item) => item.id === targetId)) return 1;
  const totalPages = Math.max(1, Math.ceil(first.total / pageSize));
  for (let page = 2; page <= totalPages; page += 1) {
    const result = await fetchPage(page);
    if (result.items.some((item) => item.id === targetId)) return page;
    if (result.items.length === 0) break;
  }
  return null;
}
