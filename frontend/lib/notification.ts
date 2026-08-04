const STORAGE_KEY = "aria2_notification_settings:v1";
const LEGACY_STORAGE_KEY = "aria2_notification_settings";

export interface NotificationSettings {
  enabled: boolean;
  onComplete: boolean;
  onError: boolean;
}

const defaultSettings: NotificationSettings = {
  enabled: false,
  onComplete: true,
  onError: true,
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function parseStoredSettings(stored: string): NotificationSettings {
  try {
    const parsed: unknown = JSON.parse(stored);
    if (!isRecord(parsed)) {
      console.warn("通知设置格式无效");
      return defaultSettings;
    }
    return {
      enabled: typeof parsed.enabled === "boolean" ? parsed.enabled : defaultSettings.enabled,
      onComplete: typeof parsed.onComplete === "boolean" ? parsed.onComplete : defaultSettings.onComplete,
      onError: typeof parsed.onError === "boolean" ? parsed.onError : defaultSettings.onError,
    };
  } catch (err) {
    console.warn("解析通知设置失败", err);
    return defaultSettings;
  }
}

export function getNotificationSettings(): NotificationSettings {
  if (typeof window === "undefined") return defaultSettings;
  let stored: string | null = null;
  try {
    stored = localStorage.getItem(STORAGE_KEY);
    if (stored === null) {
      stored = localStorage.getItem(LEGACY_STORAGE_KEY);
    }
  } catch (err) {
    console.warn("读取通知设置失败", err);
    return defaultSettings;
  }

  if (!stored) return defaultSettings;
  return parseStoredSettings(stored);
}

export function saveNotificationSettings(settings: NotificationSettings): void {
  if (typeof window === "undefined") return;
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(settings));
  } catch (err) {
    console.warn("保存通知设置失败", err);
  }
}

export async function requestNotificationPermission(): Promise<boolean> {
  if (typeof window === "undefined") return false;
  if (!("Notification" in window)) return false;
  if (Notification.permission === "granted") return true;
  if (Notification.permission === "denied") return false;
  const result = await Notification.requestPermission();
  return result === "granted";
}

function canSendNotification(): boolean {
  if (typeof window === "undefined") return false;
  if (!("Notification" in window)) return false;
  return Notification.permission === "granted";
}

function sendNotification(
  title: string,
  body: string,
  tag: string,
  onClick?: () => void,
): void {
  const settings = getNotificationSettings();
  if (!settings.enabled) return;
  if (!canSendNotification()) return;

  const notification = new Notification(title, {
    body,
    icon: "/favicon.ico",
    tag,
  });

  if (onClick) {
    notification.onclick = () => {
      window.focus();
      onClick();
      notification.close();
    };
  }
}

export function sendTaskCompleteNotification(taskName: string, taskId: number): void {
  const settings = getNotificationSettings();
  if (!settings.onComplete) return;
  sendNotification("下载完成", taskName, `aria2-complete-${taskId}`, () => {
    window.location.href = "/tasks";
  });
}

export function sendTaskErrorNotification(taskName: string, taskId: number): void {
  const settings = getNotificationSettings();
  if (!settings.onError) return;
  sendNotification("下载失败", taskName, `aria2-error-${taskId}`, () => {
    window.location.href = "/tasks";
  });
}
