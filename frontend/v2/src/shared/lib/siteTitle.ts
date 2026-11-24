import { useSyncExternalStore } from "react";

/** 站点标题（G1）：providers 拉取一次公开站点信息后写入；登录页/侧栏/标题栏共享。 */

export const DEFAULT_SITE_TITLE = "Aria2Deck";

let siteTitle = DEFAULT_SITE_TITLE;
const listeners = new Set<() => void>();

export function setSiteTitle(title: string): void {
  const next = title.trim() === "" ? DEFAULT_SITE_TITLE : title;
  if (next === siteTitle) return;
  siteTitle = next;
  for (const listener of listeners) listener();
}

export function getSiteTitle(): string {
  return siteTitle;
}

export function subscribeSiteTitle(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useSiteTitle(): string {
  return useSyncExternalStore(subscribeSiteTitle, getSiteTitle);
}
