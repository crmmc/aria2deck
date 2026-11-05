import { api } from "@/shared/api/client";
import { sha256Hex } from "@/shared/lib/crypto";
import type {
  DeleteUserResponse,
  UserListResponse,
  UserResponse,
} from "@/shared/api/types";

export const GIB = 1024 ** 3;

export function fetchUsers(page: number, pageSize: number): Promise<UserListResponse> {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  });
  return api.get<UserListResponse>(`/api/v2/users?${params.toString()}`);
}

export async function createUser(input: {
  username: string;
  password: string;
  isAdmin: boolean;
  isInitialPassword: boolean;
  quotaBytes: number | null;
}): Promise<UserResponse> {
  return api.post<UserResponse>("/api/v2/users", {
    username: input.username,
    password: await sha256Hex(input.password),
    is_admin: input.isAdmin,
    is_initial_password: input.isInitialPassword,
    quota_bytes: input.quotaBytes,
  });
}

export function updateUser(
  userId: number,
  changes: {
    username?: string;
    is_admin?: boolean;
    is_initial_password?: boolean;
    quota_bytes?: number;
  },
): Promise<UserResponse> {
  return api.patch<UserResponse>(`/api/v2/users/${userId}`, changes);
}

export function deleteUser(userId: number): Promise<DeleteUserResponse> {
  return api.del<DeleteUserResponse>(`/api/v2/users/${userId}`);
}

/** 配额 GiB ↔ bytes 换算：输入 GiB 值四舍五入到整 bytes；负值/NaN 返回 null。 */
export function gibToBytes(gib: number): number | null {
  if (!Number.isFinite(gib) || gib < 0) return null;
  return Math.round(gib * GIB);
}

export function bytesToGib(bytes: number): number {
  return Math.round((bytes / GIB) * 100) / 100;
}
