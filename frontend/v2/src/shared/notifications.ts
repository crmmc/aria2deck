/** 桌面通知链（G2/G3）：权限检查与申请、总开关+完成/失败子开关、权限不可用时降级 Toast 一次。 */

export type PermissionState = "granted" | "denied" | "default";

interface NotificationInstance {
  close: () => void;
  onclick: (() => void) | null;
}

type NotificationConstructor = (new (
  title: string,
  options?: { body?: string; tag?: string },
) => NotificationInstance) & {
  permission: PermissionState;
  requestPermission: () => Promise<PermissionState>;
};

function notificationApi(): NotificationConstructor | null {
  return (globalThis as { Notification?: NotificationConstructor }).Notification ?? null;
}

/** 申请通知权限：返回申请后的权限态（不支持返回 "unsupported"，申请失败按 "denied"）。 */
export async function requestNotificationPermission(): Promise<PermissionState | "unsupported"> {
  const impl = notificationApi();
  if (impl === null) return "unsupported";
  if (impl.permission === "granted") return "granted";
  if (impl.permission === "denied") return "denied";
  try {
    return await impl.requestPermission();
  } catch {
    return "denied";
  }
}

export interface NotificationSettings {
  enabled: boolean;
  onCompleted: boolean;
  onFailed: boolean;
}

export const DEFAULT_NOTIFICATION_SETTINGS: NotificationSettings = {
  enabled: false,
  onCompleted: true,
  onFailed: true,
};

// 读取与 useLocalState 白名单相同的键（notifications.enabled / on_completed / on_failed）。
function readBool(key: string, fallback: boolean): boolean {
  try {
    const raw = window.localStorage.getItem(key);
    if (raw === null) return fallback;
    const parsed: unknown = JSON.parse(raw);
    return typeof parsed === "boolean" ? parsed : fallback;
  } catch {
    return fallback;
  }
}

export function readNotificationSettings(): NotificationSettings {
  return {
    enabled: readBool("notifications.enabled", DEFAULT_NOTIFICATION_SETTINGS.enabled),
    onCompleted: readBool("notifications.on_completed", DEFAULT_NOTIFICATION_SETTINGS.onCompleted),
    onFailed: readBool("notifications.on_failed", DEFAULT_NOTIFICATION_SETTINGS.onFailed),
  };
}

let degradedToastShown = false;

export type TaskNotificationKind = "completed" | "failed";

/**
 * 发送任务完成/失败桌面通知：仅在「总开关 + 对应子开关」均开且权限已授予时发送；
 * 权限被拒/不可用时降级 Toast 提示一次（避免每条通知都刷屏）。
 */
export function sendTaskNotification(
  kind: TaskNotificationKind,
  taskName: string,
  taskId: number,
  fallbackToast: (kind: "error", text: string) => void,
): void {
  const settings = readNotificationSettings();
  if (!settings.enabled) return;
  if (kind === "completed" && !settings.onCompleted) return;
  if (kind === "failed" && !settings.onFailed) return;

  const impl = notificationApi();
  if (impl === null || impl.permission !== "granted") {
    if (!degradedToastShown) {
      degradedToastShown = true;
      fallbackToast("error", "桌面通知不可用，请在浏览器设置中允许通知后重试");
    }
    return;
  }

  const notification = new impl(kind === "completed" ? "下载完成" : "下载失败", {
    body: taskName,
    tag: `aria2-${kind}-${taskId}`,
  });
  notification.onclick = () => {
    window.focus();
    window.location.href = "/tasks";
    notification.close();
  };
}

/** 仅供测试：重置降级提示的一次性标记。 */
export function resetNotificationDegradeFlag(): void {
  degradedToastShown = false;
}
