import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { AppRouter } from "@/app/router";
import { goneCardFor, toShareEntryView, toShareInfoView } from "@/features/public-share/model";
import { ApiRequestError } from "@/shared/api/client";
import { triggerFormDownload } from "@/shared/lib/download";

const mockFetch = jest.fn();
global.fetch = mockFetch as unknown as typeof fetch;

jest.mock("@/shared/lib/download", () => ({
  triggerFileDownload: jest.fn(),
  triggerBatchDownloads: jest.fn(),
  triggerFormDownload: jest.fn(),
}));

type Init = { method?: string; headers?: Record<string, string>; body?: string };

function info(overrides: Record<string, unknown> = {}): Record<string, unknown> {
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

function entries(
  items: Array<{ name: string; path: string; kind: "file" | "directory"; size: number }>,
  total: number,
): Record<string, unknown> {
  return {
    items,
    total,
    page: 1,
    page_size: 200,
    breadcrumbs: [{ name: "资料", path: "" }],
  };
}

function json(status: number, body: unknown): unknown {
  return { status, ok: status >= 200 && status < 300, json: () => Promise.resolve(body) };
}

function setup(
  handler: (url: string, init?: Init) => unknown,
  options: { me?: unknown } = {},
) {
  mockFetch.mockReset();
  window.localStorage.clear();
  mockFetch.mockImplementation(async (url: string, init?: Init) => {
    if (url === "/api/v2/auth/me") {
      return options.me ?? json(401, { error: { code: "unauthenticated", message: "请先登录" } });
    }
    if (url === "/api/v2/public/site-info") {
      return json(200, {
        site_title: "Aria2Deck",
        ws_reconnect_max_delay_seconds: 30,
        ws_reconnect_jitter_seconds: 1,
        ws_reconnect_factor: 2,
      });
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
    <MemoryRouter initialEntries={["/s/abc123"]}>
      <AppProviders>
        <AppRouter />
      </AppProviders>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  (triggerFormDownload as jest.Mock).mockClear();
  window.history.replaceState(null, "", "/");
});

describe("公开分享页 · V1 单文件视图", () => {
  it("无密码分享凭分享码直连，显示 V1 卡片且不调用验密接口", async () => {
    setup(() => info());
    await screen.findByText("假期照片.zip");
    expect(document.querySelector(".glass-frame")).not.toBeNull();
    expect(screen.getByText("2.0 KB")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "下载文件" })).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: "主导航" })).toBeNull(); // 独立于认证布局。
    expect(
      mockFetch.mock.calls.some(([url, init]) => init?.method === "POST" && String(url).includes("/verify")),
    ).toBe(false);
    await waitFor(() => expect(document.title).toBe("假期照片.zip - Aria2Deck"));
  });

  it("下载按钮走 POST 表单且带 2 秒 loading 文案", async () => {
    jest.useFakeTimers();
    try {
      setup(() => info());
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });
      const button = screen.getByRole("button", { name: "下载文件" });
      fireEvent.click(button);
      expect(triggerFormDownload).toHaveBeenCalledWith("/api/s/abc123/download", {});
      expect(screen.getByRole("button", { name: "准备下载..." })).toBeDisabled();
      act(() => jest.advanceTimersByTime(2000));
      expect(screen.getByRole("button", { name: "下载文件" })).not.toBeDisabled();
      expect(window.location.search).toBe(""); // token/密码不写 URL。
      expect(window.localStorage.length).toBe(0); // 不落 localStorage。
    } finally {
      jest.useRealTimers();
    }
  });


  it("连点下载只保留一个重置计时器", async () => {
    jest.useFakeTimers();
    try {
      setup(() => info());
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });
      fireEvent.click(screen.getByRole("button", { name: "下载文件" }));
      fireEvent.click(screen.getByRole("button", { name: "准备下载..." }));
      expect(triggerFormDownload).toHaveBeenCalledTimes(1); // 禁用后的点击不重复触发。
      act(() => jest.advanceTimersByTime(2000));
      // 再次下载前先清掉上一轮的残留计时器。
      fireEvent.click(screen.getByRole("button", { name: "下载文件" }));
      expect(triggerFormDownload).toHaveBeenCalledTimes(2);
      act(() => jest.advanceTimersByTime(2000));
      expect(screen.getByRole("button", { name: "下载文件" })).not.toBeDisabled();
    } finally {
      jest.useRealTimers();
    }
  });


});

describe("公开分享页 · V1 提取码表单（手动提交，C19）", () => {
  it("密码错误时内联提示，正确后进入内容视图且 token 只在内存", async () => {
    let verifyCount = 0;
    setup((url, init) => {
      if (init?.method === "POST" && url.includes("/verify")) {
        verifyCount += 1;
        if (verifyCount === 1) {
          return json(403, { error: { code: "password_invalid", message: "分享密码错误" } });
        }
        return { access_token: "tok-en", expires_in: 1800 };
      }
      return info({ has_password: true });
    });
    expect(await screen.findByText("该分享需要提取码才能查看")).toBeInTheDocument();
    expect(screen.getByText("🔒")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("提取码"), { target: { value: "错的" } });
    fireEvent.click(screen.getByRole("button", { name: "提取文件" }));
    expect(await screen.findByText("分享密码错误")).toBeInTheDocument();
    expect(document.querySelector(".alert.alert-danger")).not.toBeNull(); // V1 错误条。

    fireEvent.change(screen.getByLabelText("提取码"), { target: { value: "对的" } });
    fireEvent.click(screen.getByRole("button", { name: "提取文件" }));
    await screen.findByRole("button", { name: "下载文件" });
    const verifyCalls = mockFetch.mock.calls.filter(
      ([url, init]) => (init as Init | undefined)?.method === "POST" && String(url).includes("/verify"),
    );
    expect(JSON.parse((verifyCalls.at(-1)![1] as Init).body as string)).toEqual({ password: "对的" });

    fireEvent.click(screen.getByRole("button", { name: "下载文件" }));
    // 验密后下载：令牌放表单隐藏域，不进 URL。
    expect(triggerFormDownload).toHaveBeenCalledWith("/api/s/abc123/download", { token: "tok-en" });
  });

  it("空密码不提交验密请求", async () => {
    setup(() => info({ has_password: true }));
    const input = await screen.findByLabelText("提取码");
    fireEvent.change(input, { target: { value: "x" } });
    fireEvent.change(input, { target: { value: "" } });
    fireEvent.submit(input.closest("form")!);
    await waitFor(() =>
      expect(
        mockFetch.mock.calls.some(([, init]) => (init as Init | undefined)?.method === "POST"),
      ).toBe(false),
    );
  });

  it("?password= 只预填不自动提交；手动验密成功后清掉地址栏参数", async () => {
    window.history.replaceState(null, "", "/s/abc123?password=s3cret");
    const replaceState = jest.spyOn(window.history, "replaceState");
    try {
      setup((url, init) => {
        if (init?.method === "POST" && url.includes("/verify")) {
          return { access_token: "tok-1", expires_in: 600 };
        }
        return info({ has_password: true });
      });
      const input = await screen.findByLabelText("提取码");
      expect(input).toHaveValue("s3cret");
      // V1 语义：URL 参数不回自动提交。
      expect(
        mockFetch.mock.calls.some(([, init]) => (init as Init | undefined)?.method === "POST"),
      ).toBe(false);
      expect(screen.getByRole("button", { name: "提取文件" })).toBeInTheDocument();

      fireEvent.click(screen.getByRole("button", { name: "提取文件" }));
      await screen.findByRole("button", { name: "下载文件" });
      await waitFor(() => expect(replaceState).toHaveBeenCalled());
      const target = String(replaceState.mock.calls.at(-1)![2]);
      expect(target).not.toContain("password");
    } finally {
      replaceState.mockRestore();
    }
  });

});

describe("公开分享页 · V1 失效与错误卡片", () => {
  it.each([
    ["share_expired", "该分享已失效", "分享链接已过期，请联系分享者重新分享", "⏰"],
    ["share_exhausted", "下载次数已用完", "该分享的下载次数已达上限", "📊"],
    ["share_revoked", "该分享已失效", "分享已撤销", "⏰"],
    ["share_unavailable", "该分享已失效", "分享文件已删除", "⏰"],
  ])("410 %s 显示 V1 失效卡", async (code, title, hint, icon) => {
    setup(() => json(410, { error: { code, message: hint } }));
    expect(await screen.findByText(title)).toBeInTheDocument();
    expect(screen.getByText(hint)).toBeInTheDocument();
    expect(screen.getByText(icon)).toBeInTheDocument();
  });

  it.each([404, 500])("%i 显示 V1「访问出错」卡", async (status) => {
    setup(() => json(status, {
      error: { code: status === 404 ? "not_found" : "internal", message: status === 404 ? "分享不存在" : "服务器内部错误" },
    }));
    expect(await screen.findByText("访问出错")).toBeInTheDocument();
    expect(screen.getByText(status === 404 ? "分享不存在" : "服务器内部错误")).toBeInTheDocument();
    expect(screen.getByText("😕")).toBeInTheDocument();
  });

  it("无效分享码直接提示，不发 info 请求", async () => {
    setup(() => info());
    render(
      <MemoryRouter initialEntries={["/s/_"]}>
        <AppProviders>
          <AppRouter />
        </AppProviders>
      </MemoryRouter>,
    );
    expect(await screen.findByText("无效的分享链接")).toBeInTheDocument();
    expect(mockFetch.mock.calls.some(([url]) => String(url) === "/api/s/_/info")).toBe(false);
  });
});

describe("公开分享页 · V1 目录浏览", () => {
  function directoryHandler(url: string, init?: Init) {
    if (init?.method === "POST" && url.includes("/verify")) {
      return json(200, { access_token: "pw-tok", expires_in: 1800 });
    }
    if (url.includes("path=docs&")) {
      return json(200, entries([{ name: "note.txt", path: "docs/note.txt", kind: "file", size: 10 }], 1));
    }
    if (url.includes("/entries")) {
      return json(
        200,
        entries(
          [
            { name: "docs", path: "docs", kind: "directory", size: 0 },
            { name: "a.pdf", path: "a.pdf", kind: "file", size: 512 },
          ],
          401,
        ),
      );
    }
    return json(200, info({ file_kind: "directory", file_name: "资料" }));
  }

  it("无密码目录直连（不带 Authorization），可进入子目录并返回上级", async () => {
    setup(directoryHandler);
    expect(await screen.findByText("资料")).toBeInTheDocument();
    expect(screen.getByText("a.pdf")).toBeInTheDocument();
    const first = mockFetch.mock.calls.find(([url]) => String(url).includes("/entries"))!;
    expect((first[1] as Init).headers?.Authorization).toBeUndefined();
    expect(screen.queryByRole("button", { name: "↵ 返回上级" })).toBeNull(); // 根目录不显示返回。

    fireEvent.click(screen.getByText("docs"));
    expect(await screen.findByText("note.txt")).toBeInTheDocument();
    expect(screen.getByText("/docs")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "↵ 返回上级" }));
    await waitFor(() => expect(screen.getByText("/.")).toBeInTheDocument());
    expect(await screen.findByText("a.pdf")).toBeInTheDocument();
  });

  it("目录项下载表单带 path（无密码时不带 token）与目录大小", async () => {
    setup(directoryHandler);
    await screen.findByText("a.pdf");
    expect(screen.getByText("512.0 B")).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("button", { name: "下载" })[0]);
    expect(triggerFormDownload).toHaveBeenCalledWith("/api/s/abc123/download", { path: "a.pdf" });
  });

  it("有密码目录：验密后带 Bearer 浏览，下载表单带 token 和 path", async () => {
    setup((url, init) => {
      if (init?.method === "POST" && url.includes("/verify")) {
        return json(200, { access_token: "pw-tok", expires_in: 1800 });
      }
      if (url.includes("/entries")) {
        return json(200, entries([{ name: "a.pdf", path: "a.pdf", kind: "file", size: 512 }], 1));
      }
      return json(200, info({ file_kind: "directory", file_name: "资料", has_password: true }));
    });
    fireEvent.change(await screen.findByLabelText("提取码"), { target: { value: "pw" } });
    fireEvent.click(screen.getByRole("button", { name: "提取文件" }));
    expect(await screen.findByText("a.pdf")).toBeInTheDocument();
    const call = mockFetch.mock.calls.filter(([url]) => String(url).includes("/entries")).at(-1)!;
    expect((call[1] as Init).headers?.Authorization).toBe("Bearer pw-tok");

    fireEvent.click(screen.getAllByRole("button", { name: "下载" })[0]);
    expect(triggerFormDownload).toHaveBeenCalledWith("/api/s/abc123/download", {
      token: "pw-tok",
      path: "a.pdf",
    });
  });

  it("目录空态与超过一页时的 V1 分页", async () => {
    setup((url) => {
      if (url.includes("path=empty&")) {
        return json(200, entries([], 0));
      }
      if (url.includes("/entries")) {
        return json(
          200,
          entries([{ name: "empty", path: "empty", kind: "directory", size: 0 }], 401),
        );
      }
      return json(200, info({ file_kind: "directory", file_name: "资料" }));
    });
    expect(await screen.findByText("empty")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("下一页")); // total 401 > 200。
    await waitFor(() =>
      expect(
        mockFetch.mock.calls.map(([url]) => String(url)).filter((url) => url.includes("/entries")).at(-1),
      ).toContain("page=2"),
    );
    await screen.findByText("empty"); // 第二页加载完成后列表重新出现。

    fireEvent.click(screen.getByText("empty"));
    expect(await screen.findByText("空文件夹")).toBeInTheDocument();
    expect(screen.getByText("/empty")).toBeInTheDocument();
  });

  it("目录加载失败内联显示，目录错误不改变分享卡片", async () => {
    setup((url) => {
      if (url.includes("/entries")) {
        return json(500, { error: { code: "internal", message: "目录读取失败" } });
      }
      return json(200, info({ file_kind: "directory", file_name: "资料" }));
    });
    expect(await screen.findByText("目录读取失败")).toBeInTheDocument();
    expect(screen.getByText("资料")).toBeInTheDocument();
  });

  it("过期目录响应报错被丢弃，不写错误条", async () => {
    const stale: { reject?: () => void } = {};
    let rootCalls = 0;
    setup((url) => {
      if (!url.includes("/entries")) {
        return json(200, info({ file_kind: "directory", file_name: "资料" }));
      }
      if (url.includes("path=docs&")) {
        return json(200, entries([{ name: "a.pdf", path: "docs/a.pdf", kind: "file", size: 1 }], 1));
      }
      rootCalls += 1;
      if (rootCalls === 2) {
        return new Promise((_resolve, reject) => {
          stale.reject = () => reject(json(500, { error: { code: "internal", message: "旧请求失败" } }));
        });
      }
      return json(200, entries([{ name: "docs", path: "docs", kind: "directory", size: 0 }], 1));
    });
    fireEvent.click(await screen.findByText("docs"));
    await screen.findByText("a.pdf");
    fireEvent.click(screen.getByRole("button", { name: "↵ 返回上级" })); // 慢请求。
    fireEvent.click(screen.getByRole("button", { name: "↵ 返回上级" })); // 快请求成为当前。
    await screen.findByText("docs");
    stale.reject?.();
    await waitFor(() => expect(screen.getByText("docs")).toBeInTheDocument());
    expect(screen.queryByText("旧请求失败")).toBeNull(); // 过期错误不覆盖当前结果。
  });

  it("Bearer 401 是页面内业务结果：目录内联报错、不跳登录页", async () => {
    setup(
      (url, init) => {
        if (init?.method === "POST" && url.includes("/verify")) {
          return json(200, { access_token: "tok", expires_in: 900 });
        }
        if (url.includes("/entries")) {
          return json(401, { error: { code: "unauthenticated", message: "访问令牌已失效" } });
        }
        return json(200, info({ file_kind: "directory", file_name: "资料", has_password: true }));
      },
      {
        me: json(200, {
          id: 1,
          username: "admin",
          is_admin: true,
          quota_bytes: 1,
          is_initial_password: false,
          pending_delete: false,
          used_bytes: 0,
          reserved_bytes: 0,
          available_bytes: 1,
        }),
      },
    );
    fireEvent.change(await screen.findByLabelText("提取码"), { target: { value: "pw" } });
    fireEvent.click(screen.getByRole("button", { name: "提取文件" }));
    // 401 只作为目录内联错误：仍留在内容视图，不跳登录页、不弹全局会话过期提示。
    expect(await screen.findByText("访问令牌已失效")).toBeInTheDocument();
    expect(screen.getByText("资料")).toBeInTheDocument();
    expect(screen.queryByText("会话已过期，请重新登录")).toBeNull();
    expect(screen.queryByText("该分享需要提取码才能查看")).toBeNull();
  });

  it("丢弃过期目录响应，只保留最后一次导航结果", async () => {
    const pending: { resolve?: (value: unknown) => void } = {};
    let rootCalls = 0;
    setup((url) => {
      if (!url.includes("/entries")) {
        return json(200, info({ file_kind: "directory", file_name: "资料" }));
      }
      if (!url.includes("path=&")) {
        return json(200, entries([{ name: "docs", path: "docs", kind: "directory", size: 0 }], 1));
      }
      rootCalls += 1;
      if (rootCalls === 2) {
        return new Promise((resolve) => {
          pending.resolve = resolve;
        });
      }
      if (rootCalls >= 3) {
        return json(200, entries([{ name: "fresh.txt", path: "fresh.txt", kind: "file", size: 1 }], 1));
      }
      return json(200, entries([{ name: "docs", path: "docs", kind: "directory", size: 0 }], 1));
    });
    await screen.findByText("docs");
    fireEvent.click(screen.getByText("docs"));
    await screen.findByText("/docs");
    // 连点两次「返回上级」产生两笔并发根目录请求，较旧的一笔晚返回。
    fireEvent.click(screen.getByRole("button", { name: "↵ 返回上级" }));
    fireEvent.click(screen.getByRole("button", { name: "↵ 返回上级" }));
    expect(await screen.findByText("fresh.txt")).toBeInTheDocument();
    pending.resolve?.(
      json(200, entries([{ name: "stale.txt", path: "stale.txt", kind: "file", size: 1 }], 1)),
    );
    await waitFor(() => expect(screen.queryByText("加载目录中...")).toBeNull());
    expect(screen.queryByText("stale.txt")).toBeNull(); // 过期响应被丢弃。
    expect(screen.getByText("fresh.txt")).toBeInTheDocument();
  });
});

describe("public-share model 映射", () => {
  it("wire → V1 视图字段", () => {
    expect(
      toShareInfoView({
        file_name: "a.zip",
        file_size: 1,
        file_kind: "directory",
        has_password: true,
        expires_at_ms: null,
        max_downloads: null,
        download_count: 0,
      }),
    ).toEqual({ file_name: "a.zip", file_size: 1, is_directory: true, has_password: true });
    expect(toShareEntryView({ name: "d", path: "d", kind: "directory", size: 0 }).is_dir).toBe(true);
    expect(toShareEntryView({ name: "f", path: "f", kind: "file", size: 3 }).is_dir).toBe(false);
  });

  it("goneCardFor 只认 410，其余交给通用错误卡", () => {
    expect(goneCardFor(new Error("boom"))).toBeNull();
    expect(goneCardFor(new ApiRequestError(404, "not_found", "分享不存在"))).toBeNull();
  });
});
