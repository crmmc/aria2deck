import { ApiRequestError } from "@/shared/api/client";
import type { FileEntryItemResponse, PublicShareInfoResponse } from "@/shared/api/types";

/** 归档参考 视图映射：把 V2 wire 形状换回 归档参考组件需要的字段，界面部分照抄 归档参考。 */

/** 分享信息视图（归档参考 `ShareInfo` 的子集，只保留 归档参考 界面实际读取的字段）。 */
export interface ShareInfoView {
  file_name: string;
  file_size: number;
  is_directory: boolean;
  has_password: boolean;
}

/** 目录项视图（对应 归档参考 `BrowseFileInfo`）。 */
export interface ShareEntryView {
  name: string;
  path: string;
  size: number;
  is_dir: boolean;
}

/** 失效提示卡文案，沿用 归档参考实现的两张卡（⏰ / 📊）与图标，不新增视觉设计。 */
export interface ShareGoneCard {
  icon: string;
  title: string;
  hint: string;
}

export function toShareInfoView(info: PublicShareInfoResponse): ShareInfoView {
  return {
    file_name: info.file_name,
    file_size: info.file_size,
    is_directory: info.file_kind === "directory",
    has_password: info.has_password,
  };
}

export function toShareEntryView(entry: FileEntryItemResponse): ShareEntryView {
  return {
    name: entry.name,
    path: entry.path,
    size: entry.size,
    is_dir: entry.kind === "directory",
  };
}

/**
 * V2 把 归档参考实现的 `is_expired/is_exhausted` 标志改成 410 错误码，
 * 页面只能按错误码恢复 归档参考 失效分支（C14：沿用既有失效展示，不新增视觉设计）。
 */
export function goneCardFor(caught: unknown): ShareGoneCard | null {
  if (!(caught instanceof ApiRequestError) || caught.status !== 410) return null;
  if (caught.code === "share_exhausted") {
    return { icon: "📊", title: "下载次数已用完", hint: "该分享的下载次数已达上限" };
  }
  if (caught.code === "share_expired") {
    return { icon: "⏰", title: "该分享已失效", hint: "分享链接已过期，请联系分享者重新分享" };
  }
  // revoked / unavailable：归档参考 没有对应状态，复用同一张失效卡，提示用后端原文。
  return { icon: "⏰", title: "该分享已失效", hint: caught.message };
}
