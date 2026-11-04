import { triggerDownloadsSequentially } from "@/lib/download-trigger";

describe("triggerDownloadsSequentially", () => {
  beforeEach(() => {
    jest.useFakeTimers();
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  test("creates hidden iframes sequentially with the configured delay", async () => {
    const promise = triggerDownloadsSequentially(["/a", "/b"], { delayMs: 1200, cleanupMs: 5000 });

    expect(document.querySelectorAll("iframe")).toHaveLength(1);
    expect(document.querySelector("iframe")?.getAttribute("src")).toBe("/a");

    await jest.advanceTimersByTimeAsync(1199);
    expect(document.querySelectorAll("iframe")).toHaveLength(1);

    await jest.advanceTimersByTimeAsync(1);
    expect(document.querySelectorAll("iframe")).toHaveLength(2);
    expect(document.querySelectorAll("iframe")[1]?.getAttribute("src")).toBe("/b");

    await promise;

    await jest.advanceTimersByTimeAsync(5000);
    expect(document.querySelectorAll("iframe")).toHaveLength(0);
  });
});
