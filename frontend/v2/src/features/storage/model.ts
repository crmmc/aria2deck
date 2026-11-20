/**
 * ST1 存储视图模型：V2 StorageFileResponse → 归档参考 存储表格视图。
 * C12：内容键按 归档参考 哈希列口径截断展示（悬停全文）；file_names 多名称「、」连接，不冒充唯一原名。
 */
import type { StorageDeleteResponse, StorageFileResponse } from "@/shared/api/types";

export interface StorageFileView {
  id: number;
  namesText: string;
  isDirectory: boolean;
  size: number;
  hashShort: string;
  hashFull: string;
  refCount: number;
  onDisk: boolean;
  createdAtMs: number;
}

/** V2 内容键形如 "file:<sha256>"，取冒号后摘要段；无前缀时整体作为摘要。 */
export function digestOf(contentKey: string): string {
  const separator = contentKey.indexOf(":");
  return separator >= 0 ? contentKey.slice(separator + 1) : contentKey;
}

/** 归档参考 哈希列：摘要截断 8 位 + "..."，完整值放 title。 */
export function shortHash(contentKey: string): string {
  return `${digestOf(contentKey).slice(0, 8)}...`;
}

export function toStorageFileView(item: StorageFileResponse): StorageFileView {
  return {
    id: item.content_id,
    namesText: item.file_names.join("、"),
    isDirectory: item.kind === "directory",
    size: item.size,
    hashShort: shortHash(item.content_key),
    hashFull: digestOf(item.content_key),
    refCount: item.reference_count,
    onDisk: item.on_disk,
    createdAtMs: item.created_at_ms,
  };
}

/** C15：202 只表示受理；取首个拒绝原因供错误提示，无拒绝返回 null。 */
export function firstRejectedMessage(response: StorageDeleteResponse): string | null {
  const rejected = response.results.find((result) => !result.accepted);
  return rejected?.error?.message ?? null;
}
