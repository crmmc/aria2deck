import type { ShareLink } from "@/types";

export type ShareFilterStatus = "all" | "active" | "expired" | "revoked";

export type SharesState = {
  records: ShareLink[];
  loading: boolean;
  selectedIds: Set<number>;
  filterStatus: ShareFilterStatus;
  searchKeyword: string;
  isOperating: boolean;
};

export type SharesAction =
  | { type: "load_start" }
  | { type: "load_success"; records: ShareLink[] }
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

export function getEffectiveStatus(record: ShareLink): string {
  const isExpiredByTime = record.expires_at ? new Date(record.expires_at) <= new Date() : false;
  const isExpiredByCount = record.max_downloads != null && record.max_downloads > 0 && record.download_count >= record.max_downloads;
  const isExpired = isExpiredByTime || isExpiredByCount;

  let currentStatus: string = record.status;
  if (currentStatus === "active" && isExpired) {
    currentStatus = "expired";
  }
  return currentStatus;
}

export function filterRecords(
  records: ShareLink[],
  searchKeyword: string,
  filterStatus: ShareFilterStatus,
): ShareLink[] {
  let filtered = records;

  if (searchKeyword.trim()) {
    const keyword = searchKeyword.toLowerCase();
    filtered = filtered.filter((r) =>
      r.file_name.toLowerCase().includes(keyword) || r.share_code.toLowerCase().includes(keyword)
    );
  }

  if (filterStatus !== "all") {
    filtered = filtered.filter((r) => getEffectiveStatus(r) === filterStatus);
  }

  return filtered;
}
