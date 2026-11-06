import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { AppRouter } from "@/app/router";
import { PublicSharePage } from "@/features/public-share/PublicSharePage";

const mockFetch = jest.fn();
global.fetch = mockFetch as unknown as typeof fetch;

jest.mock("@/shared/lib/download", () => ({
  triggerFileDownload: jest.fn(),
  triggerBatchDownloads: jest.fn(),
  triggerFormDownload: jest.fn(),
}));

import { triggerFormDownload } from "@/shared/lib/download";

function info(overrides: Record<string, unknown> = {}): unknown {
  return {
    file_name: "假期照片.zip",
    file_size: 2048,
    file_kind: "file",
    has_password: false,
    expires_at_ms: null,
    max_downloads: 5,
    download_count: 1,
    ...overrides,
  };
}

function entries(items: Array<{ name: string; path: string; kind: "file" | "directory"; size: number }>, breadcrumbs: Array<{ name: string; path: string }>): unknown {
  return { items, total: items.length, page: 1, page_size: 200, breadcrumbs };
}

function json(status: number, body: unknown): unknown {
  return { status, ok: status >= 200 && status < 300, json: () => Promise.resolve(body) };
}

function setup(code: string, handler: (url: string, init?: { method?: string; headers?: Record<string, string>; body?: string }) => unknown) {
  mockFetch.mockReset();
  mockFetch.mockImplementation(async (url: string, init?: { method?: string; headers?: Record<string, string>; body?: string }) => {
    if (url === "/api/v2/auth/me") {
      return json(401, { error: { code: "unauthenticated", message: "请先登录" } });
    }
    const result = handler(url, init);
    if (
      typeof result === "object" &&
      result !== null &&
      typeof (result as { json?: unknown }).json === "function"
    ) {
      return result;
    }
    return json(200, result);
  });
  // 经由完整路由渲染：等价于直接刷新 /s/:code 的入口路径。
  return render(
    <MemoryRouter initialEntries={[`/s/${code}`]}>
      <AppProviders>
        <AppRouter />
      </AppProviders>
    </MemoryRouter>,
  );
}

describe("PublicSharePage", () => {
  beforeEach(() => {
    (triggerFormDownload as jest.Mock).mockClear();
  });

  it("renders file info via router (direct refresh path) without login", async () => {
    setup("abc123", () => info());
    expect(await screen.findByText("假期照片.zip")).toBeInTheDocument();
    expect(screen.getByText(/文件 · 2.00 KiB · 下载 1 \/ 5 次/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "下载文件" })).toBeInTheDocument();
    expect(screen.queryByLabelText("主导航")).not.toBeInTheDocument(); // 独立于认证布局。
  });

  it.each([
    ["share_revoked", "分享已撤销", "分享者已撤销该链接"],
    ["share_expired", "分享已过期", "该分享链接已过期"],
    ["share_exhausted", "下载次数已用完", "下载次数已达上限"],
    ["share_unavailable", "分享文件已删除", "分享者已删除该文件"],
  ])("shows dedicated invalid state for %s", async (code, title, hint) => {
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url.includes("/info")) {
        return json(410, { error: { code, message: title } });
      }
      return json(404, {});
    });
    render(
      <MemoryRouter initialEntries={["/s/gone"]}>
        <AppProviders>
          <Routes>
            <Route path="/s/:code" element={<PublicSharePage />} />
          </Routes>
        </AppProviders>
      </MemoryRouter>,
    );
    expect(await screen.findByText(title)).toBeInTheDocument();
    expect(screen.getByText(new RegExp(hint))).toBeInTheDocument();
  });

  it("shows not-found state for unknown codes", async () => {
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url.includes("/info")) {
        return json(404, { error: { code: "not_found", message: "分享不存在" } });
      }
      return json(404, {});
    });
    render(
      <MemoryRouter initialEntries={["/s/nope"]}>
        <AppProviders>
          <PublicSharePage />
        </AppProviders>
      </MemoryRouter>,
    );
    expect(await screen.findByText("分享不存在")).toBeInTheDocument();
  });

  it("password form: wrong password shows 403 message inline", async () => {
    let verifyCount = 0;
    setup("pw1", (url, init) => {
      if (init?.method === "POST" && url.includes("/verify")) {
        verifyCount += 1;
        if (verifyCount === 1) {
          return json(403, { error: { code: "password_invalid", message: "分享密码错误" } });
        }
        return { access_token: "tok-en", expires_in: 1800 };
      }
      return info({ has_password: true });
    });
    expect(await screen.findByRole("button", { name: "验证并查看" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("提取密码"), { target: { value: "错的" } });
    fireEvent.click(screen.getByRole("button", { name: "验证并查看" }));
    expect(await screen.findByText("分享密码错误")).toBeInTheDocument();

    // 正确密码 → 进入内容视图，token 只留在组件内存。
    fireEvent.change(screen.getByLabelText("提取密码"), { target: { value: "对的" } });
    fireEvent.click(screen.getByRole("button", { name: "验证并查看" }));
    await screen.findByRole("button", { name: "下载文件" });
    const verifyCalls = mockFetch.mock.calls.filter(
      ([url, init]) => init?.method === "POST" && String(url).includes("/verify"),
    );
    expect(JSON.parse(verifyCalls.at(-1)![1].body as string)).toEqual({ password: "对的" });
    expect(window.location.search).toBe(""); // token/密码不写 URL。
    expect(window.localStorage.length).toBe(0); // 不落 localStorage。

    // 验密后下载：POST 表单带内存 token。
    fireEvent.click(screen.getByRole("button", { name: "下载文件" }));
    await waitFor(() =>
      expect(triggerFormDownload).toHaveBeenCalledWith("/api/s/pw1/download", { token: "tok-en" }),
    );
  });

  it("directory crumb navigation and pagination work", async () => {
    // 有密码目录：验密后浏览。
    setup("dirp", () => info({ file_kind: "directory", file_name: "资料", has_password: true }));
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (init?.method === "POST" && url.includes("/verify")) {
        return json(200, { access_token: "tok", expires_in: 1800 });
      }
      if (url.includes("path=docs")) {
        return json(200, {
          items: Array.from({ length: 3 }, (_, i) => ({
            name: `f${i}.txt`, path: `docs/f${i}.txt`, kind: "file", size: 1,
          })),
          total: 401, // 超过一页（200/页）。
          page: 1,
          page_size: 200,
          breadcrumbs: [
            { name: "资料", path: "" },
            { name: "docs", path: "docs" },
          ],
        });
      }
      if (url.includes("/entries")) {
        return json(200, {
          items: [{ name: "docs", path: "docs", kind: "directory", size: 0 }],
          total: 1, page: 1, page_size: 200,
          breadcrumbs: [{ name: "资料", path: "" }],
        });
      }
      return json(200, info({ file_kind: "directory", file_name: "资料", has_password: true }));
    });
    fireEvent.change(await screen.findByLabelText("提取密码"), { target: { value: "pw" } });
    fireEvent.click(screen.getByRole("button", { name: "验证并查看" }));
    expect(await screen.findByRole("button", { name: "进入子目录 docs" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "进入子目录 docs" }));
    await screen.findByText("f0.txt");
    fireEvent.click(screen.getByLabelText("下一页"));
    await waitFor(() => {
      const url = mockFetch.mock.calls
        .map(([u]) => String(u))
        .filter((u) => u.includes("/entries"))
        .at(-1)!;
      expect(url).toContain("page=2");
    });
    // 面包屑回到根。
    fireEvent.click(screen.getByRole("button", { name: "资料" }));
    await waitFor(() => {
      const url = mockFetch.mock.calls
        .map(([u]) => String(u))
        .filter((u) => u.includes("/entries"))
        .at(-1)!;
      expect(url).toContain("path=&");
    });
    expect(await screen.findByRole("button", { name: "进入子目录 docs" })).toBeInTheDocument();
  });

  it("renders expiry timestamp when set", async () => {
    setup("abc123", () => info({ expires_at_ms: new Date(2027, 0, 2, 3, 4, 5).getTime() }));
    expect(await screen.findByText(/2027-01-02 03:04:05 过期/)).toBeInTheDocument();
  });

  it("falls back to generic invalid state for unmapped 410 codes", async () => {
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url.includes("/info")) {
        return json(410, { error: { code: "weird_code", message: "奇怪的错误" } });
      }
      return json(404, {});
    });
    render(
      <MemoryRouter initialEntries={["/s/odd"]}>
        <AppProviders>
          <PublicSharePage />
        </AppProviders>
      </MemoryRouter>,
    );
    expect(await screen.findByText("访问出错")).toBeInTheDocument();
    expect(screen.getByText("奇怪的错误")).toBeInTheDocument();
  });

  it("passwordless single file downloads directly without token or verify", async () => {
    setup("abc123", () => info());
    expect(await screen.findByRole("button", { name: "下载文件" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "下载文件" }));
    await waitFor(() =>
      expect(triggerFormDownload).toHaveBeenCalledWith("/api/s/abc123/download", {}),
    );
    expect(
      mockFetch.mock.calls.some(([url, init]) => init?.method === "POST" && String(url).includes("/verify")),
    ).toBe(false); // 无密码分享不走验密接口。
  });

  it("passwordless directory browses directly and downloads an entry without token", async () => {
    setup("dir9", (url) => {
      if (url.includes("/entries")) {
        return entries(
          [
            { name: "docs", path: "docs", kind: "directory", size: 0 },
            { name: "a.pdf", path: "a.pdf", kind: "file", size: 512 },
          ],
          [{ name: "资料", path: "" }],
        );
      }
      return info({ file_kind: "directory", file_name: "资料" });
    });
    expect(await screen.findByText("a.pdf")).toBeInTheDocument();
    const entriesCall = mockFetch.mock.calls.find(([url]) => String(url).includes("/entries"));
    expect(String(entriesCall![0])).toContain("page_size=200");
    expect((entriesCall![1] as { headers?: Record<string, string> }).headers?.Authorization).toBeUndefined(); // 无密码直连。

    fireEvent.click(screen.getAllByRole("button", { name: "下载" })[0]);
    await waitFor(() =>
      expect(triggerFormDownload).toHaveBeenCalledWith("/api/s/dir9/download", { path: "a.pdf" }),
    );
  });

  it("passworded directory verifies then browses with Bearer token", async () => {
    setup("dirp", (url, init) => {
      if (init?.method === "POST" && url.includes("/verify")) {
        return { access_token: "pw-tok", expires_in: 1800 };
      }
      if (url.includes("/entries")) {
        return entries([{ name: "a.pdf", path: "a.pdf", kind: "file", size: 512 }], [
          { name: "资料", path: "" },
        ]);
      }
      return info({ file_kind: "directory", file_name: "资料", has_password: true });
    });
    expect(await screen.findByRole("button", { name: "验证并查看" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("提取密码"), { target: { value: "对的" } });
    fireEvent.click(screen.getByRole("button", { name: "验证并查看" }));
    expect(await screen.findByText("a.pdf")).toBeInTheDocument();
    const entriesCall = mockFetch.mock.calls
      .filter(([url]) => String(url).includes("/entries"))
      .at(-1)!;
    expect((entriesCall[1] as { headers?: Record<string, string> }).headers?.Authorization).toBe(
      "Bearer pw-tok",
    );

    fireEvent.click(screen.getAllByRole("button", { name: "下载" })[0]);
    await waitFor(() =>
      expect(triggerFormDownload).toHaveBeenCalledWith("/api/s/dirp/download", {
        token: "pw-tok",
        path: "a.pdf",
      }),
    );
  });

  it("navigates into subdirectories and shows the empty-directory state", async () => {
    let deep = false;
    setup("dir9", (url) => {
      if (url.includes("path=docs")) {
        deep = true;
        return entries([], [
          { name: "资料", path: "" },
          { name: "docs", path: "docs" },
        ]);
      }
      if (url.includes("/entries")) {
        return entries([{ name: "docs", path: "docs", kind: "directory", size: 0 }], [
          { name: "资料", path: "" },
        ]);
      }
      return info({ file_kind: "directory", file_name: "资料" });
    });
    expect(await screen.findByRole("button", { name: "进入子目录 docs" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "进入子目录 docs" }));
    await waitFor(() => expect(deep).toBe(true));
    expect(await screen.findByText("空目录")).toBeInTheDocument();
  });

  it("entries failure surfaces inline error", async () => {
    setup("dir9", (url) => {
      if (url.includes("/entries")) {
        return json(500, { error: { code: "internal", message: "目录读取失败" } });
      }
      return info({ file_kind: "directory", file_name: "资料" });
    });
    expect(await screen.findByText("目录读取失败")).toBeInTheDocument();
  });

  it("shows network error state when info fails", async () => {
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url.includes("/info")) return json(500, { error: { code: "internal", message: "后端错误" } });
      return json(401, { error: { code: "unauthenticated", message: "请先登录" } });
    });
    render(
      <MemoryRouter initialEntries={["/s/x"]}>
        <AppProviders>
          <PublicSharePage />
        </AppProviders>
      </MemoryRouter>,
    );
    expect(await screen.findByText("访问出错")).toBeInTheDocument();
    expect(screen.getByText("后端错误")).toBeInTheDocument();
  });
});

describe("PublicSharePage 分享双轨（S1+K1）与标题（K3）", () => {
  function setupWithSearch(
    path: string,
    handler: (url: string, init?: { method?: string; headers?: Record<string, string>; body?: string }) => unknown,
  ) {
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string, init?: { method?: string; headers?: Record<string, string>; body?: string }) => {
      if (url === "/api/v2/auth/me") {
        return json(401, { error: { code: "unauthenticated", message: "请先登录" } });
      }
      const result = handler(url, init);
      if (
        typeof result === "object" &&
        result !== null &&
        typeof (result as { json?: unknown }).json === "function"
      ) {
        return result;
      }
      return json(200, result);
    });
    return render(
      <MemoryRouter initialEntries={[path]}>
        <AppProviders>
          <AppRouter />
        </AppProviders>
      </MemoryRouter>,
    );
  }

  it("legacy ?password= auto-verifies once, strips the param and keeps token in memory", async () => {
    const replaceState = jest.spyOn(window.history, "replaceState").mockImplementation(() => {});
    try {
      setupWithSearch("/s/legacy?password=s3cret", (url, init) => {
        if (url.includes("/info")) return info({ has_password: true });
        if (url.includes("/verify")) {
          expect(init?.body).toBe(JSON.stringify({ password: "s3cret" }));
          return { access_token: "tok-1", expires_in: 600 };
        }
        return {};
      });
      // 自动验密成功：直接进入内容视图（不再显示验密表单）。
      expect(await screen.findByRole("button", { name: "下载文件" })).toBeInTheDocument();
      await waitFor(() => {
        expect(replaceState).toHaveBeenCalled();
        const target = String(replaceState.mock.calls[0][2]);
        expect(target).not.toContain("password");
      });
      // 验密仅一次。
      expect(mockFetch.mock.calls.filter(([url]) => String(url).includes("/verify"))).toHaveLength(1);
      // token 不进 URL：后续请求以内存 Bearer 携带。
      expect(document.baseURI).not.toContain("tok-1");
    } finally {
      replaceState.mockRestore();
    }
  });

  it("legacy ?password= failure falls back to the manual form with an inline hint", async () => {
    setupWithSearch("/s/legacy?password=bad", (url) => {
      if (url.includes("/info")) return info({ has_password: true });
      if (url.includes("/verify")) {
        return json(403, { error: { code: "password_invalid", message: "密码错误" } });
      }
      return {};
    });
    expect(await screen.findByText("链接携带的密码无效，请手动输入")).toBeInTheDocument();
    expect(screen.getByLabelText("提取密码")).toBeInTheDocument();
    // 自动验密失败后不重复尝试。
    expect(mockFetch.mock.calls.filter(([url]) => String(url).includes("/verify"))).toHaveLength(1);
  });

  it("no-password share ignores ?password= without calling verify", async () => {
    setupWithSearch("/s/open?password=whatever", (url) => {
      if (url.includes("/info")) return info({ has_password: false });
      return {};
    });
    expect(await screen.findByRole("button", { name: "下载文件" })).toBeInTheDocument();
    expect(mockFetch.mock.calls.filter(([url]) => String(url).includes("/verify"))).toHaveLength(0);
  });

  it("sets document.title to 文件名 - 站点标题 (K3)", async () => {
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url.includes("/info")) return json(200, info({ file_name: "报告.pdf" }));
      return json(200, {});
    });
    render(
      <MemoryRouter initialEntries={["/s/t1"]}>
        <AppProviders>
          <AppRouter />
        </AppProviders>
      </MemoryRouter>,
    );
    await screen.findByText("报告.pdf");
    await waitFor(() => expect(document.title).toBe("报告.pdf - Aria2Deck"));
  });
});
