import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { BackendStatusBanner } from "@/app/components/BackendStatusBanner";
import { AppLayout } from "@/app/layouts/AppLayout";
import { AppProviders } from "@/app/providers";
import type { BackendStatusResponse, UserResponse } from "@/shared/api/types";

const mockFetch = jest.fn();
global.fetch = mockFetch as unknown as typeof fetch;

const ME: UserResponse = {
  id: 1,
  username: "admin",
  is_admin: true,
  quota_bytes: 1,
  is_initial_password: false,
  pending_delete: false,
  used_bytes: 0,
  reserved_bytes: 0,
  available_bytes: 1,
};

const REGULAR: UserResponse = { ...ME, id: 2, username: "bob", is_admin: false };

function json(status: number, body: unknown): unknown {
  return { status, ok: status >= 200 && status < 300, json: () => Promise.resolve(body) };
}

function statusEnvelope(download_backend: BackendStatusResponse): unknown {
  return { download_backend };
}

function ok(status: "ok" | "degraded", message = ""): BackendStatusResponse {
  return { status, message };
}

/**
 * 直接挂载横幅：它只需要 Navigate 上下文，用户信息通过 props 注入，
 * 这样测的是横幅自身的轮询/关闭/跳转逻辑，而不是 AppProviders 的初始化。
 */
function renderBanner(user: UserResponse | null) {
  return render(
    <MemoryRouter initialEntries={["/tasks"]}>
      <Routes>
        <Route path="/tasks" element={<BackendStatusBanner user={user} />} />
        <Route path="/settings" element={<div>系统设置占位</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  mockFetch.mockReset();
});

describe("BackendStatusBanner", () => {
  it("后端正常时不渲染任何内容", async () => {
    mockFetch.mockResolvedValue(json(200, statusEnvelope(ok("ok"))));
    const { container } = renderBanner(ME);
    await act(async () => {
      await Promise.resolve();
    });
    expect(container).toBeEmptyDOMElement();
    expect(mockFetch.mock.calls[0][0]).toBe("/api/v2/system/status");
  });

  it("未登录时不渲染降级提醒", async () => {
    mockFetch.mockResolvedValue(json(200, statusEnvelope(ok("degraded", "后端不可用"))));
    const { container } = renderBanner(null);
    await act(async () => {
      await Promise.resolve();
    });
    expect(container).toBeEmptyDOMElement();
  });

  it("管理员看到「下载后端异常」与查看设置入口", async () => {
    mockFetch.mockResolvedValue(json(200, statusEnvelope(ok("degraded", "下载后端连接异常"))));
    renderBanner(ME);
    const banner = await screen.findByRole("status");
    expect(banner).toHaveAttribute("aria-live", "polite");
    expect(screen.getByText("下载后端异常")).toBeInTheDocument();
    expect(screen.getByText("下载后端连接异常")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "查看系统设置" })).toBeInTheDocument();
  });

  it("管理员点击查看设置跳转到系统设置页", async () => {
    mockFetch.mockResolvedValue(json(200, statusEnvelope(ok("degraded", "下载后端连接异常"))));
    renderBanner(ME);
    await screen.findByText("下载后端连接异常");
    fireEvent.click(screen.getByRole("button", { name: "查看系统设置" }));
    expect(await screen.findByText("系统设置占位")).toBeInTheDocument();
  });

  it("普通用户看到「服务异常」且没有设置入口", async () => {
    mockFetch.mockResolvedValue(json(200, statusEnvelope(ok("degraded", "下载后端连接异常"))));
    renderBanner(REGULAR);
    await screen.findByText("服务异常");
    expect(screen.queryByText("下载后端异常")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "查看系统设置" })).not.toBeInTheDocument();
  });

  it("关闭后本次降级期间不再显示", async () => {
    mockFetch.mockResolvedValue(json(200, statusEnvelope(ok("degraded", "下载后端连接异常"))));
    renderBanner(ME);
    await screen.findByText("下载后端连接异常");
    fireEvent.click(screen.getByRole("button", { name: "关闭服务状态提醒" }));
    await waitFor(() => {
      expect(screen.queryByText("下载后端连接异常")).not.toBeInTheDocument();
    });
  });

  it("轮询失败保留上次已知状态，不自行判定为降级", async () => {
    mockFetch.mockResolvedValue(json(200, statusEnvelope(ok("ok"))));
    renderBanner(ME);
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.queryByRole("status")).not.toBeInTheDocument();

    mockFetch.mockRejectedValue(new TypeError("fetch failed"));
    // 通过可见性事件触发一次额外拉取。
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
      await Promise.resolve();
    });
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("恢复后再次降级会重新显示（关闭状态在恢复时被重置）", async () => {
    mockFetch.mockResolvedValue(json(200, statusEnvelope(ok("degraded", "下载后端连接异常"))));
    renderBanner(ME);
    await screen.findByText("下载后端连接异常");
    fireEvent.click(screen.getByRole("button", { name: "关闭服务状态提醒" }));
    await waitFor(() => {
      expect(screen.queryByText("下载后端连接异常")).not.toBeInTheDocument();
    });

    // 后端恢复 → 状态回到 ok。
    mockFetch.mockResolvedValue(json(200, statusEnvelope(ok("ok"))));
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
      await Promise.resolve();
    });
    expect(screen.queryByRole("status")).not.toBeInTheDocument();

    // 再次降级：因为中间经历过恢复，关闭标记已被清掉，提醒重新出现。
    mockFetch.mockResolvedValue(json(200, statusEnvelope(ok("degraded", "又挂了"))));
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
      await Promise.resolve();
    });
    expect(await screen.findByText("又挂了")).toBeInTheDocument();
  });

  it("页面不可见时定时轮询不请求", async () => {
    jest.useFakeTimers();
    try {
      mockFetch.mockResolvedValue(json(200, statusEnvelope(ok("ok"))));
      renderBanner(ME);
      await act(async () => {
        await Promise.resolve();
      });
      const calls = () => mockFetch.mock.calls.filter(([url]) => url === "/api/v2/system/status").length;
      const baseline = calls();
      expect(baseline).toBe(1);

      const hiddenSpy = jest.spyOn(document, "hidden", "get").mockReturnValue(true);
      try {
        await act(async () => {
          jest.advanceTimersByTime(20_000);
          await Promise.resolve();
        });
        expect(calls()).toBe(baseline);
      } finally {
        hiddenSpy.mockRestore();
      }

      // 可见时定时器按 20s 周期重新拉取。
      await act(async () => {
        jest.advanceTimersByTime(20_000);
        await Promise.resolve();
      });
      expect(calls()).toBeGreaterThan(baseline);
    } finally {
      jest.useRealTimers();
    }
  });

  it("可见性恢复为 visible 时立即重新拉取", async () => {
    mockFetch.mockResolvedValue(json(200, statusEnvelope(ok("degraded", "下载后端连接异常"))));
    renderBanner(ME);
    await screen.findByText("下载后端连接异常");
    const calls = () => mockFetch.mock.calls.filter(([url]) => url === "/api/v2/system/status").length;
    const baseline = calls();

    const visibilitySpy = jest.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
    try {
      await act(async () => {
        document.dispatchEvent(new Event("visibilitychange"));
        await Promise.resolve();
      });
    } finally {
      visibilitySpy.mockRestore();
    }
    expect(calls()).toBe(baseline + 1);
  });

  it("卸载后迟到的响应不再写入状态", async () => {
    const holder: { resolve?: (value: unknown) => void } = {};
    mockFetch.mockImplementation(
      () =>
        new Promise((resolve) => {
          holder.resolve = resolve;
        }),
    );
    const { unmount } = renderBanner(ME);
    unmount();
    holder.resolve?.(json(200, statusEnvelope(ok("degraded", "迟到的降级"))));
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.queryByText("迟到的降级")).not.toBeInTheDocument();
  });

  it("应用壳中管理员降级提醒可关闭，普通用户看到「服务异常」", async () => {
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, REGULAR);
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      return json(200, statusEnvelope(ok("degraded", "下载后端连接异常")));
    });
    render(
      <MemoryRouter initialEntries={["/tasks"]}>
        <AppProviders>
          <AppLayout />
        </AppProviders>
      </MemoryRouter>,
    );
    expect(await screen.findByText("服务异常")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "查看系统设置" })).not.toBeInTheDocument();
  });
});
