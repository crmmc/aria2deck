/**
 * @jest-environment node
 */
import {
  getNotificationSettings,
  saveNotificationSettings,
  requestNotificationPermission,
  sendTaskCompleteNotification,
  sendTaskErrorNotification,
  type NotificationSettings,
} from "@/lib/notification";

// 在 node 环境下 window 未定义，覆盖 SSR 分支
describe("notification without window (SSR)", () => {
  it("returns defaults from getNotificationSettings", () => {
    expect(getNotificationSettings()).toEqual({
      enabled: false,
      onComplete: true,
      onError: true,
    });
  });

  it("saveNotificationSettings is a no-op", () => {
    const settings: NotificationSettings = { enabled: true, onComplete: true, onError: true };
    expect(() => saveNotificationSettings(settings)).not.toThrow();
  });

  it("requestNotificationPermission resolves false", async () => {
    await expect(requestNotificationPermission()).resolves.toBe(false);
  });

  it.each([
    ["complete", sendTaskCompleteNotification],
    ["error", sendTaskErrorNotification],
  ])("%s notification is skipped without throwing", (_name, send) => {
    expect(() => send("file.zip", 1)).not.toThrow();
  });
});
