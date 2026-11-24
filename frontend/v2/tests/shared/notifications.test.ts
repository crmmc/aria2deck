import {
  DEFAULT_NOTIFICATION_SETTINGS,
  readNotificationSettings,
  requestNotificationPermission,
  resetNotificationDegradeFlag,
  sendTaskNotification,
} from "@/shared/notifications";

interface NotificationCall {
  title: string;
  body?: string;
  tag?: string;
}

function installNotification(overrides: {
  permission?: "granted" | "denied" | "default";
  requestResult?: "granted" | "denied" | "default" | Error;
}) {
  const calls: NotificationCall[] = [];
  const instances: Array<{
    onclick: (() => void) | null;
    close: jest.Mock;
  }> = [];
  class FakeNotification {
    static permission = overrides.permission ?? "granted";
    static async requestPermission() {
      const result = overrides.requestResult ?? "granted";
      if (result instanceof Error) throw result;
      return result;
    }
    onclick: (() => void) | null = null;
    close = jest.fn();
    constructor(title: string, options?: { body?: string; tag?: string }) {
      calls.push({ title, ...options });
      instances.push(this);
    }
  }
  (globalThis as { Notification?: unknown }).Notification = FakeNotification;
  return {
    calls,
    instances,
    restore: () => delete (globalThis as { Notification?: unknown }).Notification,
  };
}

describe("requestNotificationPermission", () => {
  it("returns granted immediately when already granted", async () => {
    const impl = installNotification({ permission: "granted" });
    try {
      await expect(requestNotificationPermission()).resolves.toBe("granted");
    } finally {
      impl.restore();
    }
  });

  it("returns denied without prompting when already denied", async () => {
    const impl = installNotification({ permission: "denied", requestResult: "granted" });
    try {
      await expect(requestNotificationPermission()).resolves.toBe("denied");
    } finally {
      impl.restore();
    }
  });

  it("returns unsupported without Notification API", async () => {
    await expect(requestNotificationPermission()).resolves.toBe("unsupported");
  });

  it("maps a throwing requestPermission to denied", async () => {
    const impl = installNotification({
      permission: "default",
      requestResult: new Error("boom"),
    });
    try {
      await expect(requestNotificationPermission()).resolves.toBe("denied");
    } finally {
      impl.restore();
    }
  });
});

describe("readNotificationSettings", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("returns defaults when nothing is stored", () => {
    expect(readNotificationSettings()).toEqual(DEFAULT_NOTIFICATION_SETTINGS);
  });

  it("reads the useLocalState whitelist keys and ignores invalid values", () => {
    window.localStorage.setItem("notifications.enabled", "true");
    window.localStorage.setItem("notifications.on_completed", "false");
    window.localStorage.setItem("notifications.on_failed", '"bogus"');
    expect(readNotificationSettings()).toEqual({
      enabled: true,
      onCompleted: false,
      onFailed: true,
    });
  });

  it("falls back to defaults when stored JSON is unparseable", () => {
    window.localStorage.setItem("notifications.enabled", "{bad json");
    expect(readNotificationSettings()).toEqual(DEFAULT_NOTIFICATION_SETTINGS);
  });
});

describe("sendTaskNotification（G2/G3 发送链）", () => {
  const toast = jest.fn();

  beforeEach(() => {
    window.localStorage.clear();
    toast.mockClear();
    resetNotificationDegradeFlag();
  });

  it("sends a desktop notification when enabled, granted and sub-switch on", () => {
    const impl = installNotification({ permission: "granted" });
    try {
      window.localStorage.setItem("notifications.enabled", "true");
      sendTaskNotification("completed", "ubuntu.iso", 7, toast);
      sendTaskNotification("failed", "bad.torrent", 8, toast);
      expect(impl.calls).toEqual([
        { title: "下载完成", body: "ubuntu.iso", tag: "aria2-completed-7" },
        { title: "下载失败", body: "bad.torrent", tag: "aria2-failed-8" },
      ]);
      expect(toast).not.toHaveBeenCalled();
    } finally {
      impl.restore();
    }
  });

  it("clicking the notification focuses the window, opens tasks and closes it", () => {
    const impl = installNotification({ permission: "granted" });
    const originalFocus = window.focus;
    const originalLocation = window.location;
    try {
      window.localStorage.setItem("notifications.enabled", "true");
      const focus = jest.fn();
      window.focus = focus;
      // jsdom 的 location 不可导航：替换为可写替身以断言跳转目标。
      const locationStub = { href: "" } as Location;
      Reflect.deleteProperty(window, "location");
      Object.defineProperty(window, "location", {
        value: locationStub,
        configurable: true,
        writable: true,
      });
      sendTaskNotification("completed", "ubuntu.iso", 7, toast);
      const instance = impl.instances[0]!;
      expect(instance.onclick).toBeInstanceOf(Function);
      instance.onclick!();
      expect(focus).toHaveBeenCalledTimes(1);
      expect(locationStub.href).toBe("/tasks");
      expect(instance.close).toHaveBeenCalledTimes(1);
    } finally {
      Object.defineProperty(window, "location", {
        value: originalLocation,
        configurable: true,
        writable: true,
      });
      window.focus = originalFocus;
      impl.restore();
    }
  });

  it.each([
    ["完成子开关关闭时不发完成通知", "on_completed", "completed"],
    ["失败子开关关闭时不发失败通知", "on_failed", "failed"],
  ] as const)("%s", (_name, key, kind) => {
    const impl = installNotification({ permission: "granted" });
    try {
      window.localStorage.setItem("notifications.enabled", "true");
      window.localStorage.setItem(`notifications.${key}`, "false");
      sendTaskNotification(kind, "ubuntu.iso", 7, toast);
      expect(impl.calls).toHaveLength(0);
      expect(toast).not.toHaveBeenCalled();
    } finally {
      impl.restore();
    }
  });

  it("does nothing when the master switch is off", () => {
    const impl = installNotification({ permission: "granted" });
    try {
      sendTaskNotification("completed", "ubuntu.iso", 7, toast);
      expect(impl.calls).toHaveLength(0);
      expect(toast).not.toHaveBeenCalled();
    } finally {
      impl.restore();
    }
  });

  it("degrades to a single toast when permission is denied", () => {
    const impl = installNotification({ permission: "denied" });
    try {
      window.localStorage.setItem("notifications.enabled", "true");
      sendTaskNotification("completed", "a.iso", 1, toast);
      sendTaskNotification("failed", "b.iso", 2, toast);
      expect(impl.calls).toHaveLength(0);
      expect(toast).toHaveBeenCalledTimes(1);
      expect(toast).toHaveBeenCalledWith(
        "error",
        "桌面通知不可用，请在浏览器设置中允许通知后重试",
      );
    } finally {
      impl.restore();
    }
  });

  it("degrades once when Notification API is unavailable", () => {
    window.localStorage.setItem("notifications.enabled", "true");
    sendTaskNotification("completed", "a.iso", 1, toast);
    sendTaskNotification("failed", "b.iso", 2, toast);
    expect(toast).toHaveBeenCalledTimes(1);
  });
});
