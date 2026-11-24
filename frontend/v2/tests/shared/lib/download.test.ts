import {
  triggerBatchDownloads,
  triggerFileDownload,
  triggerFormDownload,
} from "@/shared/lib/download";

describe("triggerFileDownload", () => {
  it("creates an anchor with download attribute, clicks and removes it", () => {
    const created: HTMLElement[] = [];
    const click = jest.fn();
    const remove = jest.fn();
    const appendChild = jest.spyOn(document.body, "appendChild").mockImplementation((node) => {
      created.push(node as HTMLElement);
      return node;
    });
    const anchorSpy = jest.spyOn(document, "createElement").mockImplementation((tag) => {
      if (tag === "a") {
        return { click, remove, set href(_v: string) {}, set rel(_v: string) {}, set download(_v: string) {} } as unknown as HTMLElement;
      }
      return document.createElement(tag);
    });
    try {
      triggerFileDownload("/api/v2/files/3/download", "报告.pdf");
      expect(created).toHaveLength(1);
      expect(click).toHaveBeenCalledTimes(1);
      expect(remove).toHaveBeenCalledTimes(1);
    } finally {
      anchorSpy.mockRestore();
      appendChild.mockRestore();
    }
  });
});

describe("triggerBatchDownloads", () => {
  beforeEach(() => jest.useFakeTimers());
  afterEach(() => jest.useRealTimers());

  it("creates a hidden iframe per url with 800ms interval", async () => {
    const iframes: HTMLElement[] = [];
    const realCreate = document.createElement.bind(document);
    const createElement = jest
      .spyOn(document, "createElement")
      .mockImplementation((tag: string) => realCreate(tag));
    const appendChild = jest.spyOn(document.body, "appendChild").mockImplementation((node) => {
      iframes.push(node as HTMLElement);
      return node;
    });
    const urls = ["/d/1", "/d/2", "/d/3"];
    try {
      const promise = triggerBatchDownloads(urls);
      expect(iframes).toHaveLength(1); // 第一个立即触发。
      expect(createElement).toHaveBeenCalledWith("iframe");
      expect((iframes[0] as HTMLIFrameElement).src).toContain("/d/1");
      expect((iframes[0] as HTMLElement).style.display).toBe("none");

      await jest.advanceTimersByTimeAsync(800);
      expect(iframes).toHaveLength(2);
      await jest.advanceTimersByTimeAsync(800);
      expect(iframes).toHaveLength(3);
      await promise;
    } finally {
      appendChild.mockRestore();
      createElement.mockRestore();
    }
  });

  it("single url resolves without interval", async () => {
    const appendChild = jest.spyOn(document.body, "appendChild").mockImplementation((node) => node);
    try {
      await triggerBatchDownloads(["/only"]);
      expect(appendChild).toHaveBeenCalledTimes(1);
    } finally {
      appendChild.mockRestore();
    }
  });
});

describe("triggerFormDownload", () => {
  it("builds a POST form with hidden token and path fields, submits and removes", () => {
    const form = document.createElement("form");
    const submit = jest.fn();
    const remove = jest.fn();
    const realCreate = document.createElement.bind(document);
    const appendChild = jest.spyOn(document.body, "appendChild").mockImplementation((node) => node);
    const createElement = jest.spyOn(document, "createElement").mockImplementation((tag) => {
      if (tag === "form") {
        return Object.assign(form, { submit, remove }) as HTMLFormElement;
      }
      return realCreate(tag);
    });
    try {
      triggerFormDownload("/api/s/abc/download", { token: "t0k", path: "docs/a.txt" });
      expect(form.method.toLowerCase()).toBe("post");
      expect(form.target).toBe("_blank"); // C20：保持 V1 新窗口行为。
      expect(form.action).toContain("/api/s/abc/download");
      expect(submit).toHaveBeenCalledTimes(1);
      expect(remove).toHaveBeenCalledTimes(1);
      const inputs = Array.from(form.querySelectorAll("input"));
      expect(inputs.map((i) => `${i.name}=${i.value}`)).toEqual([
        "token=t0k",
        "path=docs/a.txt",
      ]);
    } finally {
      createElement.mockRestore();
      appendChild.mockRestore();
    }
  });

  it("builds a token-less form for passwordless shares", () => {
    const form = document.createElement("form");
    const submit = jest.fn();
    const realCreate = document.createElement.bind(document);
    const appendChild = jest.spyOn(document.body, "appendChild").mockImplementation((node) => node);
    const createElement = jest.spyOn(document, "createElement").mockImplementation((tag) => {
      if (tag === "form") return Object.assign(form, { submit }) as HTMLFormElement;
      return realCreate(tag);
    });
    try {
      triggerFormDownload("/api/s/abc/download", {});
      expect(form.querySelectorAll("input")).toHaveLength(0); // 无密码直连：无隐藏域。
      expect(submit).toHaveBeenCalledTimes(1);
    } finally {
      createElement.mockRestore();
      appendChild.mockRestore();
    }
  });

  it("omits the path field when not provided", () => {
    const form = document.createElement("form");
    const submit = jest.fn();
    const realCreate = document.createElement.bind(document);
    const appendChild = jest.spyOn(document.body, "appendChild").mockImplementation((node) => node);
    const createElement = jest.spyOn(document, "createElement").mockImplementation((tag) => {
      if (tag === "form") return Object.assign(form, { submit }) as HTMLFormElement;
      return realCreate(tag);
    });
    try {
      triggerFormDownload("/api/s/abc/download", { token: "t" });
      expect(form.querySelectorAll("input")).toHaveLength(1);
      expect(form.querySelector("input")!.name).toBe("token");
      expect(submit).toHaveBeenCalledTimes(1);
    } finally {
      createElement.mockRestore();
      appendChild.mockRestore();
    }
  });
});
