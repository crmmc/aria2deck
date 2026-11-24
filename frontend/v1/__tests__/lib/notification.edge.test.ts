import {
  getNotificationSettings,
  sendTaskCompleteNotification,
} from "@/lib/notification";

const STORAGE_KEY = "aria2_notification_settings:v1";

function stubLocalStorage(store: Record<string, string>) {
  Object.defineProperty(window, "localStorage", {
    value: {
      getItem: (key: string) => store[key] ?? null,
      setItem: (key: string, value: string) => {
        store[key] = value;
      },
      removeItem: (key: string) => {
        delete store[key];
      },
    },
    writable: true,
    configurable: true,
  });
}

describe("notification edge cases", () => {
  let store: Record<string, string>;
  let warnSpy: jest.SpyInstance;
  const notificationConstructor = jest.fn();

  beforeEach(() => {
    store = {};
    stubLocalStorage(store);
    warnSpy = jest.spyOn(console, "warn").mockImplementation(() => {});
    notificationConstructor.mockClear();

    Object.defineProperty(window, "Notification", {
      value: Object.assign(notificationConstructor, { permission: "granted" }),
      writable: true,
      configurable: true,
    });
  });

  afterEach(() => {
    warnSpy.mockRestore();
  });

  describe.each([
    ["null", "null"],
    ["number", "42"],
    ["string", '"text"'],
  ])("non-object stored settings (%s)", (_name, stored) => {
    it("returns defaults and warns", () => {
      store[STORAGE_KEY] = stored;

      expect(getNotificationSettings()).toEqual({
        enabled: false,
        onComplete: true,
        onError: true,
      });
      expect(warnSpy).toHaveBeenCalledWith("通知设置格式无效");
    });
  });

  it("falls back per-field when stored enabled is not a boolean", () => {
    store[STORAGE_KEY] = JSON.stringify({ enabled: "yes", onComplete: false, onError: false });

    expect(getNotificationSettings()).toEqual({
      enabled: false,
      onComplete: false,
      onError: false,
    });
  });

  it("skips sending when Notification API is missing even if enabled", () => {
    store[STORAGE_KEY] = JSON.stringify({
      enabled: true,
      onComplete: true,
      onError: true,
    });
    delete (window as { Notification?: unknown }).Notification;

    expect(() => sendTaskCompleteNotification("file.zip", 1)).not.toThrow();
    expect(notificationConstructor).not.toHaveBeenCalled();
  });
});
