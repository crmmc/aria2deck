import {
  getNotificationSettings,
  saveNotificationSettings,
  requestNotificationPermission,
  sendTaskCompleteNotification,
  sendTaskErrorNotification,
  type NotificationSettings,
} from "@/lib/notification";

const STORAGE_KEY = "aria2_notification_settings";

describe("notification", () => {
  let localStorageMock: {
    store: Record<string, string>;
    getItem: jest.Mock;
    setItem: jest.Mock;
    removeItem: jest.Mock;
    clear: jest.Mock;
  };

  let mockNotificationInstance: {
    close: jest.Mock;
    onclick: ((event: Event) => void) | null;
  };

  let MockNotification: jest.Mock & {
    permission: NotificationPermission;
    requestPermission: jest.Mock;
  };

  beforeEach(() => {
    localStorageMock = {
      store: {},
      getItem: jest.fn((key: string) => localStorageMock.store[key] || null),
      setItem: jest.fn((key: string, value: string) => {
        localStorageMock.store[key] = value;
      }),
      removeItem: jest.fn((key: string) => {
        delete localStorageMock.store[key];
      }),
      clear: jest.fn(() => {
        localStorageMock.store = {};
      }),
    };

    Object.defineProperty(window, "localStorage", {
      value: localStorageMock,
      writable: true,
    });

    mockNotificationInstance = {
      close: jest.fn(),
      onclick: null,
    };

    MockNotification = jest.fn().mockImplementation(() => mockNotificationInstance) as jest.Mock & {
      permission: NotificationPermission;
      requestPermission: jest.Mock;
    };
    MockNotification.permission = "default";
    MockNotification.requestPermission = jest.fn().mockResolvedValue("granted");

    Object.defineProperty(window, "Notification", {
      value: MockNotification,
      writable: true,
      configurable: true,
    });

    window.focus = jest.fn();
  });

  afterEach(() => {
    jest.clearAllMocks();
  });

  describe("getNotificationSettings", () => {
    it("returns defaults when localStorage is empty", () => {
      const settings = getNotificationSettings();
      expect(settings).toEqual({
        enabled: false,
        onComplete: true,
        onError: true,
      });
    });

    it("returns stored settings when present", () => {
      const stored: NotificationSettings = {
        enabled: true,
        onComplete: false,
        onError: true,
      };
      localStorageMock.store[STORAGE_KEY] = JSON.stringify(stored);

      const settings = getNotificationSettings();
      expect(settings).toEqual(stored);
    });

    it("handles invalid JSON gracefully", () => {
      localStorageMock.store[STORAGE_KEY] = "invalid json";
      const warnSpy = jest.spyOn(console, "warn").mockImplementation(() => {});

      const settings = getNotificationSettings();
      expect(settings).toEqual({
        enabled: false,
        onComplete: true,
        onError: true,
      });
      expect(warnSpy).toHaveBeenCalled();
      warnSpy.mockRestore();
    });

    it("merges partial stored settings with defaults", () => {
      localStorageMock.store[STORAGE_KEY] = JSON.stringify({ enabled: true });

      const settings = getNotificationSettings();
      expect(settings).toEqual({
        enabled: true,
        onComplete: true,
        onError: true,
      });
    });
  });

  describe("saveNotificationSettings", () => {
    it("saves to localStorage", () => {
      const settings: NotificationSettings = {
        enabled: true,
        onComplete: true,
        onError: false,
      };

      saveNotificationSettings(settings);

      expect(localStorageMock.setItem).toHaveBeenCalledWith(
        STORAGE_KEY,
        JSON.stringify(settings)
      );
    });

    it("handles localStorage write errors gracefully", () => {
      localStorageMock.setItem.mockImplementation(() => {
        throw new Error("quota exceeded");
      });
      const warnSpy = jest.spyOn(console, "warn").mockImplementation(() => {});

      expect(() =>
        saveNotificationSettings({
          enabled: true,
          onComplete: true,
          onError: true,
        })
      ).not.toThrow();

      expect(warnSpy).toHaveBeenCalled();
      warnSpy.mockRestore();
    });
  });

  it("returns defaults when localStorage read throws", () => {
    localStorageMock.getItem.mockImplementation(() => {
      throw new Error("storage blocked");
    });
    const warnSpy = jest.spyOn(console, "warn").mockImplementation(() => {});

    expect(getNotificationSettings()).toEqual({
      enabled: false,
      onComplete: true,
      onError: true,
    });

    expect(warnSpy).toHaveBeenCalled();
    warnSpy.mockRestore();
  });

  describe("requestNotificationPermission", () => {
    it("returns true when permission is granted", async () => {
      MockNotification.permission = "granted";

      const result = await requestNotificationPermission();
      expect(result).toBe(true);
    });

    it("returns false when permission is denied", async () => {
      MockNotification.permission = "denied";

      const result = await requestNotificationPermission();
      expect(result).toBe(false);
    });

    it("requests permission when default and returns result", async () => {
      MockNotification.permission = "default";
      MockNotification.requestPermission.mockResolvedValue("granted");

      const result = await requestNotificationPermission();
      expect(MockNotification.requestPermission).toHaveBeenCalled();
      expect(result).toBe(true);
    });

    it("returns false when permission request is denied", async () => {
      MockNotification.permission = "default";
      MockNotification.requestPermission.mockResolvedValue("denied");

      const result = await requestNotificationPermission();
      expect(result).toBe(false);
    });

    it("returns false when Notification API not available", async () => {
      const originalNotification = window.Notification;
      delete (window as { Notification?: unknown }).Notification;

      const result = await requestNotificationPermission();
      expect(result).toBe(false);

      Object.defineProperty(window, "Notification", {
        value: originalNotification,
        writable: true,
        configurable: true,
      });
    });
  });

  describe("sendTaskCompleteNotification", () => {
    beforeEach(() => {
      MockNotification.permission = "granted";
    });

    it("creates notification when enabled and onComplete is true", () => {
      localStorageMock.store[STORAGE_KEY] = JSON.stringify({
        enabled: true,
        onComplete: true,
        onError: true,
      });

      sendTaskCompleteNotification("test-file.zip", 123);

      expect(MockNotification).toHaveBeenCalledWith("下载完成", expect.objectContaining({
        body: "test-file.zip",
      }));
    });

    it("clicking completion notification redirects to tasks page", () => {
      localStorageMock.store[STORAGE_KEY] = JSON.stringify({
        enabled: true,
        onComplete: true,
        onError: true,
      });
      const errorSpy = jest.spyOn(console, "error").mockImplementation(() => {});

      sendTaskCompleteNotification("test-file.zip", 123);
      mockNotificationInstance.onclick?.(new Event("click"));

      expect(window.focus).toHaveBeenCalled();
      expect(mockNotificationInstance.close).toHaveBeenCalled();
      errorSpy.mockRestore();
    });

    it("does not create notification when disabled", () => {
      localStorageMock.store[STORAGE_KEY] = JSON.stringify({
        enabled: false,
        onComplete: true,
        onError: true,
      });

      sendTaskCompleteNotification("test-file.zip", 123);

      expect(MockNotification).not.toHaveBeenCalled();
    });

    it("does not create notification when onComplete is false", () => {
      localStorageMock.store[STORAGE_KEY] = JSON.stringify({
        enabled: true,
        onComplete: false,
        onError: true,
      });

      sendTaskCompleteNotification("test-file.zip", 123);

      expect(MockNotification).not.toHaveBeenCalled();
    });

    it("does not create notification when permission not granted", () => {
      MockNotification.permission = "denied";
      localStorageMock.store[STORAGE_KEY] = JSON.stringify({
        enabled: true,
        onComplete: true,
        onError: true,
      });

      sendTaskCompleteNotification("test-file.zip", 123);

      expect(MockNotification).not.toHaveBeenCalled();
    });
  });

  describe("sendTaskErrorNotification", () => {
    beforeEach(() => {
      MockNotification.permission = "granted";
    });

    it("creates notification when enabled and onError is true", () => {
      localStorageMock.store[STORAGE_KEY] = JSON.stringify({
        enabled: true,
        onComplete: true,
        onError: true,
      });

      sendTaskErrorNotification("test-file.zip", 123);

      expect(MockNotification).toHaveBeenCalledWith("下载失败", expect.objectContaining({
        body: "test-file.zip",
      }));
    });

    it("clicking error notification redirects to tasks page", () => {
      localStorageMock.store[STORAGE_KEY] = JSON.stringify({
        enabled: true,
        onComplete: true,
        onError: true,
      });
      const errorSpy = jest.spyOn(console, "error").mockImplementation(() => {});

      sendTaskErrorNotification("test-file.zip", 123);
      mockNotificationInstance.onclick?.(new Event("click"));

      expect(mockNotificationInstance.close).toHaveBeenCalled();
      errorSpy.mockRestore();
    });

    it("does not create notification when disabled", () => {
      localStorageMock.store[STORAGE_KEY] = JSON.stringify({
        enabled: false,
        onComplete: true,
        onError: true,
      });

      sendTaskErrorNotification("test-file.zip", 123);

      expect(MockNotification).not.toHaveBeenCalled();
    });

    it("does not create notification when onError is false", () => {
      localStorageMock.store[STORAGE_KEY] = JSON.stringify({
        enabled: true,
        onComplete: true,
        onError: false,
      });

      sendTaskErrorNotification("test-file.zip", 123);

      expect(MockNotification).not.toHaveBeenCalled();
    });
  });
});
