import { api } from "@/shared/api/client";
import type {
  PublicEntryListResponse,
  PublicShareInfoResponse,
  ShareVerifyResponse,
} from "@/shared/api/types";

/** 公开分享功能 API：info / verify / entries 三个端点，token 由调用方以内存参数传入。 */

// 目录浏览分页页大小（与后端 BROWSE_DEFAULT_PAGE_SIZE 保持一致）
export const BROWSE_PAGE_SIZE = 200;

export function fetchShareInfo(code: string): Promise<PublicShareInfoResponse> {
  return api.get<PublicShareInfoResponse>(`/api/s/${encodeURIComponent(code)}/info`);
}

export function verifyShare(code: string, password: string): Promise<ShareVerifyResponse> {
  return api.post<ShareVerifyResponse>(`/api/s/${encodeURIComponent(code)}/verify`, { password });
}

/** 无密码分享凭分享码直连（不带 Authorization）；有密码分享带内存 Bearer 令牌。 */
export function browseShare(
  code: string,
  token: string,
  path: string,
  page: number,
): Promise<PublicEntryListResponse> {
  const query = new URLSearchParams({
    path,
    page: String(page),
    page_size: String(BROWSE_PAGE_SIZE),
  });
  return api.get<PublicEntryListResponse>(
    `/api/s/${encodeURIComponent(code)}/entries?${query.toString()}`,
    token === "" ? undefined : { headers: { Authorization: `Bearer ${token}` } },
  );
}
