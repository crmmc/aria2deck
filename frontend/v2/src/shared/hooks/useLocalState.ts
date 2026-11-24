import { useCallback, useEffect, useState } from "react";

/** WEB-13 本地状态白名单：仅批准的键可写 localStorage；不可用时回退内存并提示一次。 */

export type LocalStateKey =
  | "tasks.filter"
  | "notifications.enabled"
  | "notifications.on_completed"
  | "notifications.on_failed";

const STORAGE_ERROR_MESSAGE = "浏览器本地存储不可用，设置将不会保留";

let warned = false;

function isLocalStorageAvailable(): boolean {
  try {
    const probe = "__aria2deck_probe__";
    window.localStorage.setItem(probe, "1");
    window.localStorage.removeItem(probe);
    return true;
  } catch {
    return false;
  }
}

export interface LocalState<T> {
  value: T;
  setValue: (value: T) => void;
  persistent: boolean;
}

export function useLocalState<T>(key: LocalStateKey, initial: T): LocalState<T> {
  const available = isLocalStorageAvailable();
  const [value, setValue] = useState<T>(() => {
    if (!available) return initial;
    try {
      const raw = window.localStorage.getItem(key);
      return raw === null ? initial : (JSON.parse(raw) as T);
    } catch {
      return initial;
    }
  });

  useEffect(() => {
    if (available) return;
    if (warned) return;
    warned = true;
    // 回退提示：一次性、非阻塞。
    console.warn(STORAGE_ERROR_MESSAGE);
  }, [available]);

  const persist = useCallback(
    (next: T) => {
      setValue(next);
      if (!available) return;
      try {
        window.localStorage.setItem(key, JSON.stringify(next));
      } catch {
        /* 写失败按不可用处理 */
      }
    },
    [available, key],
  );

  return { value, setValue: persist, persistent: available };
}
