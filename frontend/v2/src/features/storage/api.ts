import { api } from "@/shared/api/client";
import type {
  StorageDeleteResponse,
  StorageFileListResponse,
  StorageReferencingUserListResponse,
} from "@/shared/api/types";

export function fetchStorageFiles(
  page: number,
  pageSize: number,
  search: string,
  orphansOnly: boolean,
): Promise<StorageFileListResponse> {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  });
  const term = search.trim();
  if (term !== "") params.set("search", term);
  if (orphansOnly) params.set("orphans_only", "true");
  return api.get<StorageFileListResponse>(`/api/v2/storage/files?${params.toString()}`);
}

export function fetchReferencingUsers(
  contentId: number,
): Promise<StorageReferencingUserListResponse> {
  return api.get<StorageReferencingUserListResponse>(
    `/api/v2/storage/files/${contentId}/users`,
  );
}

export function deleteStorageFiles(contentIds: number[]): Promise<StorageDeleteResponse> {
  return api.post<StorageDeleteResponse>("/api/v2/storage/files/delete", {
    content_ids: contentIds,
  });
}
