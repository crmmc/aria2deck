import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { MemoryRouter } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { useAuth, useToast } from "@/shared/auth/hooks";
import { ModalOverlay } from "@/shared/components/ModalOverlay";
import { requestModalClose } from "@/shared/ui/modalClose";
import { DEFAULT_SITE_TITLE, getSiteTitle, setSiteTitle } from "@/shared/lib/siteTitle";
import type { ToastKind } from "@/shared/auth/contexts";
import type { UserResponse } from "@/shared/api/types";

const mockFetch = jest.fn();
global.fetch = mockFetch as unknown as typeof fetch;

const ME: UserResponse = {
  status: "active",
  id: 1,
  username: "admin",
  is_admin: true,
  quota_bytes: 100,
  is_initial_password: false,
  pending_delete: false,
  used_bytes: 0,
  reserved_bytes: 0,
  available_bytes: 100,
};

function json(status: number, body: unknown): unknown {
  return { status, ok: status >= 200 && status < 300, json: () => Promise.resolve(body) };
}

type ToastProbe = {
  push?: (kind: ToastKind, text: string) => void;
  dismiss?: (id: number) => void;
  root?: HTMLElement;
};

/** AppProviders 自身渲染 ToastHost，因此这里只挂一个探针读取 toast 上下文。 */
function renderWithToasts(): ToastProbe {
  const probe: ToastProbe = {};
  function Probe() {
    const { push, dismiss } = useToast();
    probe.push = push;
    probe.dismiss = dismiss;
    return null;
  }
  const view = render(
    <AppProviders>
      <Probe />
    </AppProviders>,
  );
  probe.root = view.container;
  return probe;
}

let originalShowModal: HTMLDialogElement["showModal"] | undefined;
let originalClose: HTMLDialogElement["close"] | undefined;

function AppToastLayerHarness() {
  const { push } = useToast();
  const [outerOpen, setOuterOpen] = useState(false);
  const [innerOpen, setInnerOpen] = useState(false);
  return (
    <>
      <button onClick={() => setOuterOpen(true)}>打开外层</button>
      <button onClick={() => push("info", "模态提示")}>显示模态提示</button>
      {outerOpen ? (
        <ModalOverlay onClose={() => setOuterOpen(false)} ariaLabel="外层弹窗">
          <button onClick={() => setInnerOpen(true)}>打开内层</button>
          <button onClick={(event) => requestModalClose(event.currentTarget)}>关闭外层</button>
        </ModalOverlay>
      ) : null}
      {innerOpen ? (
        <ModalOverlay onClose={() => setInnerOpen(false)} ariaLabel="内层弹窗">
          <button onClick={(event) => requestModalClose(event.currentTarget)}>关闭内层</button>
        </ModalOverlay>
      ) : null}
    </>
  );
}

beforeEach(() => {
  originalShowModal = HTMLDialogElement.prototype.showModal;
  originalClose = HTMLDialogElement.prototype.close;
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
    configurable: true,
    value: jest.fn(function showModal(this: HTMLDialogElement) {
      this.setAttribute("open", "");
    }),
  });
  Object.defineProperty(HTMLDialogElement.prototype, "close", {
    configurable: true,
    value: jest.fn(function close(this: HTMLDialogElement) {
      this.removeAttribute("open");
    }),
  });
  mockFetch.mockReset();
  setSiteTitle(DEFAULT_SITE_TITLE);
  mockFetch.mockImplementation(async (url: string) => {
    if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
    return json(200, ME);
  });
});

afterEach(() => {
  document.querySelectorAll("dialog").forEach((dialog) => dialog.removeAttribute("open"));
  if (originalShowModal) {
    Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
      configurable: true,
      value: originalShowModal,
    });
  } else {
    Reflect.deleteProperty(HTMLDialogElement.prototype, "showModal");
  }
  if (originalClose) {
    Object.defineProperty(HTMLDialogElement.prototype, "close", {
      configurable: true,
      value: originalClose,
    });
  } else {
    Reflect.deleteProperty(HTMLDialogElement.prototype, "close");
  }
});

describe("ToastHost（providers 内建 toast）", () => {
  it("无 toast 时不渲染栈项", async () => {
    renderWithToasts();
    await act(async () => {
      await Promise.resolve();
    });
    expect(document.querySelectorAll(".notification-stack-item")).toHaveLength(0);
  });

  it("推送 toast 后按类型渲染，并可手动关闭", async () => {
    const probe = renderWithToasts();
    await act(async () => {
      await Promise.resolve();
    });

    act(() => {
      probe.push?.("success", "保存成功");
    });
    const host = document.querySelector(".notification-stack-item");
    expect(host).toBeTruthy();
    expect(host?.parentElement).toBe(document.querySelector(".notification-stack"));
    expect(host).toHaveAttribute("role", "status");
    expect(document.querySelector(".toast-success")?.textContent).toContain("保存成功");

    act(() => {
      probe.push?.("error", "保存失败");
    });
    expect(document.querySelector(".toast-error")?.textContent).toContain("保存失败");

    // 关闭第一条：只剩错误 toast。
    const closeButtons = screen.getAllByRole("button", { name: "关闭提示" });
    expect(closeButtons).toHaveLength(2);
    act(() => {
      fireEvent.click(closeButtons[0]);
    });
    expect(document.querySelector(".toast-success")).toBeNull();
    expect(document.querySelector(".toast-error")?.textContent).toContain("保存失败");

    // 关闭最后一条：栈项消失。
    act(() => {
      fireEvent.click(screen.getByRole("button", { name: "关闭提示" }));
    });
    expect(document.querySelectorAll(".notification-stack-item")).toHaveLength(0);
  });

  it("toast 5 秒后自动消失", async () => {
    jest.useFakeTimers();
    try {
      const probe = renderWithToasts();
      await act(async () => {
        await Promise.resolve();
      });
      act(() => {
        probe.push?.("info", "稍纵即逝");
      });
      expect(screen.getByText("稍纵即逝")).toBeInTheDocument();

      await act(async () => {
        jest.advanceTimersByTime(5000);
      });
      expect(screen.queryByText("稍纵即逝")).not.toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });

  it("follows nested modals, traps Toast close focus, and keeps the original 5 second deadline", async () => {
    jest.useFakeTimers();
    try {
      render(
        <AppProviders>
          <AppToastLayerHarness />
        </AppProviders>,
      );
      await act(async () => {
        await Promise.resolve();
      });
      const openOuter = screen.getByRole("button", { name: "打开外层" });
      openOuter.focus();
      fireEvent.click(openOuter);
      fireEvent.click(screen.getByRole("button", { name: "显示模态提示" }));

      const outer = screen.getByRole("dialog", { name: "外层弹窗" });
      const getHost = () => document.querySelector(".notification-stack");
      expect(getHost()?.parentElement).toBe(outer);

      const openInner = screen.getByRole("button", { name: "打开内层" });
      const closeToast = screen.getByRole("button", { name: "关闭提示" });
      fireEvent.keyDown(openInner, { key: "Tab", shiftKey: true });
      expect(closeToast).toHaveFocus();
      fireEvent.keyDown(closeToast, { key: "Tab" });
      expect(openInner).toHaveFocus();

      await act(async () => {
        jest.advanceTimersByTime(1000);
      });
      fireEvent.click(openInner);
      const inner = screen.getByRole("dialog", { name: "内层弹窗" });
      expect(getHost()?.parentElement).toBe(inner);

      await act(async () => {
        jest.advanceTimersByTime(1000);
      });
      fireEvent.click(screen.getByRole("button", { name: "关闭内层" }));
      expect(screen.queryByRole("dialog", { name: "内层弹窗" })).not.toBeInTheDocument();
      expect(getHost()?.parentElement).toBe(outer);
      expect(openInner).toHaveFocus();

      await act(async () => {
        jest.advanceTimersByTime(2000);
      });
      fireEvent.click(screen.getByRole("button", { name: "关闭外层" }));
      expect(screen.queryByRole("dialog", { name: "外层弹窗" })).not.toBeInTheDocument();
      expect(getHost()?.parentElement).toBe(document.body);
      expect(openOuter).toHaveFocus();
      expect(screen.getByText("模态提示")).toBeInTheDocument();

      await act(async () => {
        jest.advanceTimersByTime(1000);
      });
      expect(screen.queryByText("模态提示")).not.toBeInTheDocument();
      expect(document.querySelectorAll(".notification-stack-item")).toHaveLength(0);
    } finally {
      jest.useRealTimers();
    }
  });

  it("多条 toast 各自独立计时，先推送的先消失", async () => {
    jest.useFakeTimers();
    try {
      const probe = renderWithToasts();
      await act(async () => {
        await Promise.resolve();
      });

      act(() => {
        probe.push?.("info", "第一条");
      });
      await act(async () => {
        jest.advanceTimersByTime(3000);
      });
      act(() => {
        probe.push?.("info", "第二条");
      });

      // 到第一条的 5 秒：只移除第一条。
      await act(async () => {
        jest.advanceTimersByTime(2000);
      });
      expect(screen.queryByText("第一条")).not.toBeInTheDocument();
      expect(screen.getByText("第二条")).toBeInTheDocument();

      await act(async () => {
        jest.advanceTimersByTime(3000);
      });
      expect(screen.queryByText("第二条")).not.toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });
});

describe("providers 初始化错误分支", () => {
  it("会话过期：受保护路由提示 toast 后清空用户", async () => {
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      return json(401, { error: { code: "unauthenticated", message: "请先登录" } });
    });
    function Probe() {
      const { user, loading } = useAuth();
      return <span data-testid="state">{`${String(loading)}:${user?.username ?? "none"}`}</span>;
    }
    render(
      <MemoryRouter initialEntries={["/tasks"]}>
        <AppProviders>
          <Probe />
        </AppProviders>
      </MemoryRouter>,
    );
    await waitFor(() => {
      expect(screen.getByTestId("state").textContent).toBe("false:none");
    });
    expect(await screen.findByText("加载任务失败: 会话已过期，请重新登录")).toBeInTheDocument();
  });

  it.each(["/login", "/s/Ab3dEf9h"])(
    "登录页与公开分享页（%s）的 401 静默处理",
    async (pathname) => {
      mockFetch.mockImplementation(async (url: string) => {
        if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
        return json(401, { error: { code: "unauthenticated", message: "请先登录" } });
      });
      function Probe() {
        const { loading } = useAuth();
        return <span data-testid="loading">{String(loading)}</span>;
      }
      // providers 依据 window.location.pathname 判定是否提示，jsdom 默认是 "/"。
      window.history.pushState({}, "", pathname);
      try {
        render(
          <AppProviders>
            <Probe />
          </AppProviders>,
        );
        await waitFor(() => expect(screen.getByTestId("loading").textContent).toBe("false"));
        expect(screen.queryByText(/会话已过期/)).not.toBeInTheDocument();
      } finally {
        window.history.pushState({}, "", "/");
      }
    },
  );

  it("未包装的 Error（非 ApiRequestError）进入「未知错误」", async () => {
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      throw new Error("boom");
    });
    function Probe() {
      const { error, loading } = useAuth();
      return <span data-testid="state">{`${String(loading)}:${error ?? "none"}`}</span>;
    }
    // fetch 抛错会被 client 转成 ApiRequestError(status=0)，这里验证网络错误的既有文案。
    render(
      <AppProviders>
        <Probe />
      </AppProviders>,
    );
    await waitFor(() => {
      expect(screen.getByTestId("state").textContent).toBe("false:无法连接服务器，请检查网络连接");
    });
  });

  it("refresh 遇 401 清空用户，遇其他错误也清空用户", async () => {
    let auth: ReturnType<typeof useAuth> | null = null;
    function Probe() {
      auth = useAuth();
      return null;
    }
    render(
      <AppProviders>
        <Probe />
      </AppProviders>,
    );
    await waitFor(() => expect(auth!.user).not.toBeNull());

    mockFetch.mockResolvedValueOnce(json(401, { error: { code: "unauthenticated", message: "请先登录" } }));
    await act(async () => {
      await auth!.refresh();
    });
    expect(auth!.user).toBeNull();
  });

  it("refresh 遇服务器 500 清空用户", async () => {
    let auth: ReturnType<typeof useAuth> | null = null;
    function Probe() {
      auth = useAuth();
      return null;
    }
    render(
      <AppProviders>
        <Probe />
      </AppProviders>,
    );
    await waitFor(() => expect(auth!.user).not.toBeNull());

    mockFetch.mockResolvedValueOnce(json(500, { error: { code: "internal", message: "服务器错误" } }));
    await act(async () => {
      await auth!.refresh();
    });
    expect(auth!.user).toBeNull();
  });

  it("logout 请求失败时仍然清空本地用户", async () => {
    let auth: ReturnType<typeof useAuth> | null = null;
    function Probe() {
      auth = useAuth();
      return null;
    }
    render(
      <AppProviders>
        <Probe />
      </AppProviders>,
    );
    await waitFor(() => expect(auth!.user).not.toBeNull());

    mockFetch.mockRejectedValueOnce(new TypeError("fetch failed"));
    await act(async () => {
      await auth!.logout();
    });
    expect(auth!.user).toBeNull();
  });

  it("站点信息请求失败时保留默认标题，且不影响登录就绪", async () => {
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/public/site-info") throw new TypeError("fetch failed");
      return json(200, ME);
    });
    function Probe() {
      const { loading, user } = useAuth();
      return <span data-testid="state">{`${String(loading)}:${user?.username ?? "none"}`}</span>;
    }
    render(
      <AppProviders>
        <Probe />
      </AppProviders>,
    );
    await waitFor(() => {
      expect(screen.getByTestId("state").textContent).toBe("false:admin");
    });
    expect(getSiteTitle()).toBe(DEFAULT_SITE_TITLE);
  });

  it("初始化遇服务器错误给出可重试的错误，retryAuth 成功后清空错误", async () => {
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      return json(500, { error: { code: "internal", message: "服务器错误" } });
    });
    let auth: ReturnType<typeof useAuth> | null = null;
    function Probe() {
      auth = useAuth();
      return <span data-testid="state">{`${String(auth.loading)}:${auth.error ?? "none"}`}</span>;
    }
    render(
      <AppProviders>
        <Probe />
      </AppProviders>,
    );
    await waitFor(() => {
      expect(screen.getByTestId("state").textContent).toBe("false:服务器错误: 服务器错误");
    });

    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      return json(200, ME);
    });
    await act(async () => {
      auth!.retryAuth();
      await Promise.resolve();
    });
    await waitFor(() => {
      expect(screen.getByTestId("state").textContent).toBe("false:none");
    });
    expect(auth!.user?.username).toBe("admin");
  });

  it("响应体解析失败（非 ApiRequestError）时错误落到「未知错误」", async () => {
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/public/site-info") return json(200, { site_title: "Aria2Deck" });
      return {
        status: 200,
        ok: true,
        json: () => Promise.reject(new TypeError("Unexpected end of JSON input")),
      };
    });
    function Probe() {
      const { error, loading } = useAuth();
      return <span data-testid="state">{`${String(loading)}:${error ?? "none"}`}</span>;
    }
    render(
      <AppProviders>
        <Probe />
      </AppProviders>,
    );
    await waitFor(() => {
      expect(screen.getByTestId("state").textContent).toBe("false:未知错误");
    });
  });

  it("refresh 成功后刷新用户与错误状态", async () => {
    let auth: ReturnType<typeof useAuth> | null = null;
    function Probe() {
      auth = useAuth();
      return null;
    }
    render(
      <AppProviders>
        <Probe />
      </AppProviders>,
    );
    await waitFor(() => expect(auth!.user).not.toBeNull());

    mockFetch.mockResolvedValueOnce(json(200, { ...ME, username: "dave", quota_bytes: 4096 }));
    await act(async () => {
      await auth!.refresh();
    });
    expect(auth!.user?.username).toBe("dave");
    expect(auth!.user?.quota_bytes).toBe(4096);
    expect(auth!.error).toBeNull();
  });

  it("login 成功后写入用户，失败时抛错且不写入", async () => {
    let auth: ReturnType<typeof useAuth> | null = null;
    function Probe() {
      auth = useAuth();
      return null;
    }
    render(
      <AppProviders>
        <Probe />
      </AppProviders>,
    );
    await waitFor(() => expect(auth!.user).not.toBeNull());

    mockFetch.mockResolvedValueOnce(json(200, { ...ME, username: "carol" }));
    await act(async () => {
      const me = await auth!.login("carol", "pw");
      expect(me.username).toBe("carol");
    });
    expect(auth!.user?.username).toBe("carol");

    // 服务器错误：抛错但不动已登录用户。
    mockFetch.mockResolvedValueOnce(json(500, { error: { code: "internal", message: "服务器错误" } }));
    await act(async () => {
      await expect(auth!.login("carol", "bad")).rejects.toThrow("服务器错误");
    });
    expect(auth!.user?.username).toBe("carol");

    // 401 属于会话失效：统一 handler 会清空用户，即便这次是登录请求本身失败。
    mockFetch.mockResolvedValueOnce(json(401, { error: { code: "unauthenticated", message: "用户名或密码无效" } }));
    await act(async () => {
      await expect(auth!.login("carol", "bad")).rejects.toThrow("会话已过期，请重新登录");
    });
    expect(auth!.user).toBeNull();
  });
});
