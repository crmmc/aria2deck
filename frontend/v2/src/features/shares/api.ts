import { api } from "@/shared/api/client";
import type {
  ShareBatchResponse,
  ShareItemResponse,
  ShareItemStatus,
  ShareListResponse,
  ShareRevokeAllResponse,
} from "@/shared/api/types";

export async function fetchShares(
  status: ShareItemStatus | null,
  page: number,
  pageSize: number,
  search: string,
): Promise<ShareListResponse> {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  });
  if (status !== null) params.set("status", status);
  const term = search.trim();
  if (term !== "") params.set("q", term);
  return api.get<ShareListResponse>(`/api/v2/shares?${params.toString()}`);
}

export async function revokeShares(shareIds: number[]): Promise<ShareBatchResponse> {
  return api.post<ShareBatchResponse>("/api/v2/shares/revoke", { share_ids: shareIds });
}

export async function deleteShares(shareIds: number[]): Promise<ShareBatchResponse> {
  return api.del<ShareBatchResponse>("/api/v2/shares", { share_ids: shareIds });
}

export async function revokeAllShares(): Promise<ShareRevokeAllResponse> {
  return api.del<ShareRevokeAllResponse>("/api/v2/shares/all");
}

/** 复制文本：无密码 = 链接；有密码 = 链接 + 密码行（V2 后端不回传密码，用占位提示）。 */
export function shareCopyText(
  share: Pick<ShareItemResponse, "code" | "has_password">,
  password?: string | null,
): string {
  const link = `${window.location.origin}/s/${share.code}`;
  if (!share.has_password) return link;
  const secret = password ?? "（请向分享者索取）";
  return `${link}\n密码：${secret}`;
}
