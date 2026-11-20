/**
 * SH1 分享视图模型：V2 ShareItemResponse → 归档参考 ShareCard 视图。
 * C14：exhausted/unavailable 等 V2 新增状态集中映射到 归档参考 既有失效展示语义，不新增视觉设计。
 * C21：全量聚合全部 V2 页后本地筛选/选择（归档参考 无分页界面）。
 * C35：V2 分页按 id 升序，归档参考 列表按 id 降序，差异在 sortShareRecords 内吸收。
 */
import type { ShareItemResponse, ShareItemStatus } from "@/shared/api/types";

export type ShareEffectiveStatus = "active" | "expired" | "revoked";

export type ShareFilterStatus = "all" | ShareEffectiveStatus;

export interface ShareRecordView {
  id: number;
  shareCode: string;
  fileName: string;
  fileSize: number;
  hasPassword: boolean;
  expiresAtMs: number | null;
  maxDownloads: number | null;
  downloadCount: number;
  createdAtMs: number;
  /** C14 映射后的基础状态。 */
  status: ShareEffectiveStatus;
}

/**
 * C14：V2 状态归一为 归档参考实现的三态展示，不新增视觉。
 * exhausted 对应 归档参考 getEffectiveStatus 里「下载次数达标即已过期」的既有语义（归档参考 后端只发 active/revoked）；
 * unavailable（文件待删除）与 revoked 在 归档参考 都只能表达为「已失效」。
 */
export function toShareEffectiveStatus(status: ShareItemStatus): ShareEffectiveStatus {
  if (status === "active") return "active";
  if (status === "expired" || status === "exhausted") return "expired";
  return "revoked";
}

export function toShareRecordView(item: ShareItemResponse): ShareRecordView {
  return {
    id: item.id,
    shareCode: item.code,
    fileName: item.file_name,
    fileSize: item.file_size,
    hasPassword: item.has_password,
    expiresAtMs: item.expires_at_ms,
    maxDownloads: item.max_downloads,
    downloadCount: item.download_count,
    createdAtMs: item.created_at_ms,
    status: toShareEffectiveStatus(item.status),
  };
}

/** 归档参考 getEffectiveStatus：active 叠加本地过期（到期时间或下载次数达标）后视为 expired。 */
export function getEffectiveStatus(record: ShareRecordView): ShareEffectiveStatus {
  const isExpiredByTime = record.expiresAtMs !== null && record.expiresAtMs <= Date.now();
  const isExpiredByCount =
    record.maxDownloads !== null &&
    record.maxDownloads > 0 &&
    record.downloadCount >= record.maxDownloads;
  if (record.status === "active" && (isExpiredByTime || isExpiredByCount)) return "expired";
  return record.status;
}

export type SharesState = {
  records: ShareRecordView[];
  loading: boolean;
  selectedIds: Set<number>;
  filterStatus: ShareFilterStatus;
  searchKeyword: string;
  isOperating: boolean;
};

export type SharesAction =
  | { type: "load_start" }
  | { type: "load_success"; records: ShareRecordView[] }
  | { type: "load_finish" }
  | { type: "set_filter_status"; status: ShareFilterStatus }
  | { type: "set_search_keyword"; keyword: string }
  | { type: "toggle_selected"; id: number }
  | { type: "set_selected"; ids: number[] }
  | { type: "clear_selected" }
  | { type: "set_operating"; operating: boolean };

export const initialSharesState: SharesState = {
  records: [],
  loading: true,
  selectedIds: new Set(),
  filterStatus: "all",
  searchKeyword: "",
  isOperating: false,
};

export function sharesReducer(state: SharesState, action: SharesAction): SharesState {
  switch (action.type) {
    case "load_start":
      return { ...state, loading: true };
    case "load_success":
      return { ...state, records: action.records };
    case "load_finish":
      return { ...state, loading: false };
    case "set_filter_status":
      return { ...state, filterStatus: action.status };
    case "set_search_keyword":
      return { ...state, searchKeyword: action.keyword };
    case "toggle_selected": {
      const next = new Set(state.selectedIds);
      if (next.has(action.id)) {
        next.delete(action.id);
      } else {
        next.add(action.id);
      }
      return { ...state, selectedIds: next };
    }
    case "set_selected":
      return { ...state, selectedIds: new Set(action.ids) };
    case "clear_selected":
      return { ...state, selectedIds: new Set() };
    case "set_operating":
      return { ...state, isOperating: action.operating };
  }
}

/** 归档参考 list_shares 口径：id 降序（新分享在前）；V2 分页按 id 升序，需在功能边界翻转。 */
export function sortShareRecords(records: ShareRecordView[]): ShareRecordView[] {
  return [...records].sort((a, b) => b.id - a.id);
}

export function filterRecords(
  records: ShareRecordView[],
  searchKeyword: string,
  filterStatus: ShareFilterStatus,
): ShareRecordView[] {
  let filtered = records;

  if (searchKeyword.trim()) {
    const keyword = searchKeyword.toLowerCase();
    filtered = filtered.filter(
      (r) =>
        r.fileName.toLowerCase().includes(keyword) ||
        r.shareCode.toLowerCase().includes(keyword),
    );
  }

  if (filterStatus !== "all") {
    filtered = filtered.filter((r) => getEffectiveStatus(r) === filterStatus);
  }

  return filtered;
}
