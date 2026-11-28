/**
 * F1 文件功能模型：归档参考 视图形状、排序规则、搜索命中映射与逐页定位。
 */
import type { FileItemResponse, FileSearchHitResponse } from "@/shared/api/types";

export type SortField = "name" | "size" | "created_at" | "type";
export type SortOrder = "asc" | "desc";

export const FOLDER_TYPE_LABEL = "文件夹";
const COMPOUND_EXTENSIONS = ["tar.gz", "tar.bz2", "tar.xz", "tar.zst"];

/** 文件类型列的显示文本：白名单取复合后缀，其余取最后一段，无后缀为 "-"。 */
export function fileTypeLabel(name: string, isDirectory: boolean): string {
  if (isDirectory) return FOLDER_TYPE_LABEL;
  const lower = name.toLowerCase();
  for (const compound of COMPOUND_EXTENSIONS) {
    if (lower.endsWith(`.${compound}`)) return compound;
  }
  const dotIndex = lower.lastIndexOf(".");
  if (dotIndex <= 0 || dotIndex === lower.length - 1) return "-";
  return lower.slice(dotIndex + 1);
}

export type FilterField = "type" | "size";
export type FilterRule = "eq" | "ne" | "gt" | "lt";

export interface FilterCondition {
  id: number;
  field: FilterField;
  rule: FilterRule;
  /** 未选指标时为 ""，条件不生效。 */
  value: string;
}

export interface FilterRuleDef {
  value: FilterRule;
  label: string;
}

export const FIELD_RULES: Record<FilterField, readonly FilterRuleDef[]> = {
  type: [
    { value: "eq", label: "=" },
    { value: "ne", label: "≠" },
  ],
  size: [
    { value: "gt", label: "≥" },
    { value: "lt", label: "<" },
  ],
};

const MB = 1024 * 1024;
const GB = 1024 * MB;
export const SIZE_PRESETS = [
  { value: String(10 * MB), label: "10 MB" },
  { value: String(100 * MB), label: "100 MB" },
  { value: String(500 * MB), label: "500 MB" },
  { value: String(GB), label: "1 GB" },
] as const;

/** 当前页去重类型：文件夹在前，其余按字母序。 */
export function distinctTypeLabels(files: readonly FileView[]): string[] {
  const labels = new Set(files.map((file) => fileTypeLabel(file.name, file.isDirectory)));
  return [...labels].sort((a, b) => {
    if (a === FOLDER_TYPE_LABEL) return -1;
    if (b === FOLDER_TYPE_LABEL) return 1;
    return a.localeCompare(b);
  });
}

export function matchesCondition(
  item: Pick<FileView, "name" | "size" | "isDirectory">,
  condition: Pick<FilterCondition, "field" | "rule" | "value">,
): boolean {
  if (condition.value === "") return true;
  if (condition.field === "type") {
    const label = fileTypeLabel(item.name, item.isDirectory);
    return condition.rule === "eq" ? label === condition.value : label !== condition.value;
  }
  if (item.isDirectory) return false;
  const bytes = Number(condition.value);
  return condition.rule === "gt" ? item.size >= bytes : item.size < bytes;
}

/** 类型的「=」全局唯一：两个 = 条件必然为空集或重复。 */
export function isRuleDisabled(
  conditions: readonly FilterCondition[],
  rowIndex: number,
  field: FilterField,
  rule: FilterRule,
): boolean {
  if (field !== "type" || rule !== "eq") return false;
  return conditions.some(
    (condition, index) =>
      index !== rowIndex && condition.field === "type" && condition.rule === "eq" && condition.value !== "",
  );
}

export function defaultRule(
  conditions: readonly FilterCondition[],
  rowIndex: number,
  field: FilterField,
): FilterRule {
  const rules = FIELD_RULES[field];
  return (rules.find((rule) => !isRuleDisabled(conditions, rowIndex, field, rule.value)) ?? rules[0]).value;
}

/** 指标禁选：完全重复的条件禁选；大小上下限互斥（必然空集）禁选。 */
export function isValueDisabled(
  conditions: readonly FilterCondition[],
  rowIndex: number,
  condition: Pick<FilterCondition, "field" | "rule">,
  candidate: string,
): boolean {
  const others = conditions.filter(
    (other, index) => index !== rowIndex && other.field === condition.field && other.value !== "",
  );
  if (others.some((other) => other.rule === condition.rule && other.value === candidate)) return true;
  if (condition.field !== "size") return false;
  let lo = -Infinity;
  let hi = Infinity;
  for (const other of others) {
    const bytes = Number(other.value);
    if (other.rule === "gt") lo = Math.max(lo, bytes);
    else hi = Math.min(hi, bytes);
  }
  if (condition.rule === "gt") return Number(candidate) >= hi;
  return Number(candidate) <= lo;
}

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

/** 目录项 key：相对当前文件夹的完整路径，根目录下就是文件名本身。 */
export function browseItemKey(path: readonly string[], name: string): string {
  return [...path, name].join("/");
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
    else if (field === "type") {
      cmp = fileTypeLabel(a.name, a.isDirectory).localeCompare(fileTypeLabel(b.name, b.isDirectory))
        || a.name.localeCompare(b.name);
    } else if (field === "size") cmp = a.size - b.size;
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
    let cmp: number;
    if (effective === "name") cmp = a.name.localeCompare(b.name);
    else if (effective === "type") {
      cmp = fileTypeLabel(a.name, a.isDirectory).localeCompare(fileTypeLabel(b.name, b.isDirectory))
        || a.name.localeCompare(b.name);
    } else cmp = a.size - b.size;
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
