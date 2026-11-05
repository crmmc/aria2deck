/** URL 构造：下载链接走 Cookie 会话（文件库）或公开页下载端点（批次 2 使用）。 */

export function fileDownloadUrl(fileId: number, path?: string): string {
  const query = path ? `?path=${encodeURIComponent(path)}` : "";
  return `/api/v2/files/${fileId}/download${query}`;
}

export function publicShareDownloadUrl(code: string): string {
  return `/api/s/${encodeURIComponent(code)}/download`;
}

export function apiPath(path: string): string {
  return path.startsWith("/") ? path : `/${path}`;
}
