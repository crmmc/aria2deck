import { api } from "@/shared/api/client";
import { fileDownloadUrl } from "@/shared/api/urls";
import type {
  FileEntryListResponse,
  FileItemResponse,
  FileListResponse,
  FileSearchResponse,
  PackListResponse,
  PackPrecomputeResponse,
  PackViewResponse,
  ShareCreateOptions,
  ShareItemResponse,
} from "@/shared/api/types";

export const ROOT_PAGE_SIZE = 20;
export const ENTRY_PAGE_SIZE = 200;

export async function fetchFiles(page: number, pageSize: number): Promise<FileListResponse> {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  });
  return api.get<FileListResponse>(`/api/v2/files?${params.toString()}`);
}

export async function searchFiles(
  term: string,
  scopeFileId: number | null,
): Promise<FileSearchResponse> {
  const params = new URLSearchParams({ search: term });
  if (scopeFileId !== null) params.set("scope", String(scopeFileId));
  return api.get<FileSearchResponse>(`/api/v2/files?${params.toString()}`);
}

export async function fetchEntries(
  fileId: number,
  path: string,
  page: number,
  pageSize: number,
): Promise<FileEntryListResponse> {
  const params = new URLSearchParams({
    path,
    page: String(page),
    page_size: String(pageSize),
  });
  return api.get<FileEntryListResponse>(`/api/v2/files/${fileId}/entries?${params.toString()}`);
}

export async function renameFile(fileId: number, name: string): Promise<FileItemResponse> {
  return api.patch<FileItemResponse>(`/api/v2/files/${fileId}`, { name });
}

export async function deleteFile(fileId: number): Promise<void> {
  await api.del(`/api/v2/files/${fileId}`);
}

export async function createShare(
  userFileId: number,
  options: ShareCreateOptions,
): Promise<ShareItemResponse> {
  return api.post<ShareItemResponse>("/api/v2/shares", {
    user_file_id: userFileId,
    expires_in_seconds: options.expires_in_seconds ?? null,
    max_downloads: options.max_downloads ?? null,
    password: options.password ?? null,
  });
}

export async function precomputePack(
  userFileIds: number[],
): Promise<PackPrecomputeResponse> {
  return api.post<PackPrecomputeResponse>("/api/v2/packs/precompute", { items: userFileIds });
}

export async function createPack(
  userFileIds: number[],
  outputName: string,
  deleteSource: boolean,
): Promise<PackViewResponse> {
  return api.post<PackViewResponse>("/api/v2/packs", {
    items: userFileIds,
    output_name: outputName,
    delete_source: deleteSource,
  });
}

export async function fetchPack(packId: number): Promise<PackViewResponse> {
  return api.get<PackViewResponse>(`/api/v2/packs/${packId}`);
}

/** F1 打包任务卡：本人最近任务（新任务在前）。 */
export async function fetchPacks(limit: number): Promise<PackListResponse> {
  return api.get<PackListResponse>(`/api/v2/packs?limit=${limit}`);
}

export async function cancelPack(packId: number): Promise<void> {
  await api.post(`/api/v2/packs/${packId}/cancel`);
}

export async function deletePackRecord(packId: number): Promise<void> {
  await api.del(`/api/v2/packs/${packId}/record`);
}

export { fileDownloadUrl };
