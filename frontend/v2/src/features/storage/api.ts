import { api } from "@/shared/api/client";
import type {
  DiskOrphanListResponse,
  QueueDeleteResultResponse,
  QueueEnqueueResultResponse,
  QueueSnapshotResponse,
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

export function fetchDiskOrphans(
  page: number,
  pageSize: number,
  search: string,
): Promise<DiskOrphanListResponse> {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  });
  const term = search.trim();
  if (term !== "") params.set("search", term);
  return api.get<DiskOrphanListResponse>(
    `/api/v2/storage/disk-orphans?${params.toString()}`,
  );
}

export function fetchAdmissionQueue(): Promise<QueueSnapshotResponse> {
  return api.get<QueueSnapshotResponse>("/api/v2/storage/disk-orphans/queue");
}

export function enqueueDiskOrphans(
  storagePaths: string[],
): Promise<QueueEnqueueResultResponse[]> {
  return api.post<QueueEnqueueResultResponse[]>("/api/v2/storage/disk-orphans/queue", {
    storage_paths: storagePaths,
  });
}

export function deleteDiskOrphans(
  storagePaths: string[],
): Promise<QueueDeleteResultResponse[]> {
  return api.del<QueueDeleteResultResponse[]>("/api/v2/storage/disk-orphans/queue", {
    storage_paths: storagePaths,
  });
}

export function startAdmissionQueue(): Promise<QueueSnapshotResponse> {
  return api.post<QueueSnapshotResponse>("/api/v2/storage/disk-orphans/queue/start");
}

export function stopAdmissionQueue(): Promise<QueueSnapshotResponse> {
  return api.post<QueueSnapshotResponse>("/api/v2/storage/disk-orphans/queue/stop");
}
