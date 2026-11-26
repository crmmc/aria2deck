import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { MemoryRouter } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { ProfilePage } from "@/features/profile/ProfilePage";
import type { RpcAccessResponse, UserResponse } from "@/shared/api/types";

const mockFetch = jest.fn();
global.fetch = mockFetch as unknown as typeof fetch;

const ME: UserResponse = {
  status: "active",
  id: 1,
  username: "alice",
  is_admin: false,
  quota_bytes: 1,
  is_initial_password: false,
  pending_delete: false,
  used_bytes: 0,
  reserved_bytes: 0,
  available_bytes: 1,
};

const ME_INITIAL: UserResponse = { ...ME, is_initial_password: true };

const RPC_ON: RpcAccessResponse = {
  enabled: true,
  secret: "raw-secret-value",
};
const RPC_OFF: RpcAccessResponse = { enabled: false, secret: null };

const RPC_URL = `${window.location.origin}/aria2/jsonrpc`;

function json(status: number, body: unknown): unknown {
  return { status, ok: status >= 200 && status < 300, json: () => Promise.resolve(body) };
}

function setup(
  handler: (url: string, init?: { method?: string; body?: string }) => unknown,
  me: UserResponse = ME,
) {
  mockFetch.mockReset();
  mockFetch.mockImplementation(async (url: string, init?: { method?: string; body?: string }) => {
    if (url === "/api/v2/auth/me") return json(200, me);
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
}

function renderPage(initialEntry = "/profile") {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <AppProviders>
        <ProfilePage />
      </AppProviders>
    </MemoryRouter>,
  );
}

/** 页面可卸载、Provider 保持挂载：用于验证卸载后到达的响应不产生副作用。 */
function renderPageWithToggle(initialEntry = "/profile") {
  function Harness() {
    const [visible, setVisible] = useState(true);
    return (
      <>
        <button type="button" onClick={() => setVisible(false)}>
          卸载页面
        </button>
        {visible && <ProfilePage />}
      </>
    );
  }
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <AppProviders>
        <Harness />
      </AppProviders>
    </MemoryRouter>,
  );
}

function passwordInput(label: string): HTMLInputElement {
  return screen.getByLabelText(label) as HTMLInputElement;
}

let originalShowModal: HTMLDialogElement["showModal"] | undefined;
let originalClose: HTMLDialogElement["close"] | undefined;
let consoleError: jest.SpyInstance;

beforeEach(() => {
  window.localStorage.clear();
  // 组件在卸载后仍报 console.error 属于「卸载守卫失效」的可观测证据。
  consoleError = jest.spyOn(console, "error").mockImplementation(() => undefined);
  delete (globalThis as { Notification?: unknown }).Notification;
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
  delete (globalThis as { Notification?: unknown }).Notification;
  consoleError.mockRestore();
});

describe("ProfilePage 渲染（V1 四分区）", () => {
  it("渲染页头与修改密码/浏览器通知/外部访问三个分区", async () => {
    setup(() => RPC_ON);
    renderPage();
    expect(await screen.findByRole("heading", { name: "用户设置" })).toBeInTheDocument();
    expect(screen.getByText("个人偏好与外部访问")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "修改密码" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "浏览器通知" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "外部访问" })).toBeInTheDocument();
    // 非初始密码用户显示当前密码输入框。
    expect(passwordInput("当前密码")).toBeInTheDocument();
    expect(screen.getByText("至少 6 位字符")).toBeInTheDocument();
  });

  it("RPC 读取完成前不渲染页面内容", async () => {
    let release: (value: unknown) => void = () => undefined;
    const pending = new Promise((resolve) => {
      release = resolve;
    });
    setup((url) => {
      if (url === "/api/v2/credentials/rpc") return pending as never;
      return RPC_ON;
    });
    renderPage();
    expect(screen.queryByRole("heading", { name: "用户设置" })).not.toBeInTheDocument();
    release(json(200, RPC_ON));
    expect(await screen.findByRole("heading", { name: "用户设置" })).toBeInTheDocument();
  });
});

describe("ProfilePage 初始密码提示", () => {
  it("URL 带 ?initial_password=1 时弹出提醒，点「我知道了」关闭", async () => {
    setup(() => RPC_ON);
    renderPage("/profile?initial_password=1");
    const dialog = await screen.findByRole("dialog", { name: "初始密码安全提醒" });
    expect(
      within(dialog).getByText(/您当前使用的是管理员设置的初始密码/),
    ).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "我知道了" }));
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "初始密码安全提醒" })).not.toBeInTheDocument(),
    );
  });

  it("URL 无参数时不弹提醒", async () => {
    setup(() => RPC_ON);
    renderPage();
    await screen.findByRole("heading", { name: "用户设置" });
    expect(screen.queryByText(/初始密码/)).not.toBeInTheDocument();
  });
});

describe("ProfilePage 修改密码", () => {
  it("提交 PBKDF2 派生后的新旧密码，成功提示并清空表单", async () => {
    setup(() => RPC_ON);
    renderPage();
    await screen.findByRole("heading", { name: "用户设置" });
    fireEvent.change(passwordInput("当前密码"), { target: { value: "oldpw" } });
    fireEvent.change(passwordInput("新密码"), { target: { value: "newpw123" } });
    fireEvent.change(passwordInput("确认新密码"), { target: { value: "newpw123" } });
    fireEvent.click(screen.getByRole("button", { name: "修改密码" }));
    await waitFor(
      () => {
        const call = mockFetch.mock.calls.find(([url]) => url === "/api/v2/auth/password");
        expect(call).toBeDefined();
        const body = JSON.parse(call![1].body as string);
        // 独立基准（username=alice）：pbkdf2_hmac('sha256', pw, sha256(b'alice').hexdigest().encode(), 10000).hex()
        expect(body.old_password).toBe("d8b20a1f89e56c1cde5b162e3b10ed180b01d5711a8cbf29e2b1018ac7b35d01");
        expect(body.new_password).toBe("253af49750a3f4cd54a44ced05695cdc4fd34e9d9461dccd4d5a5fd08815d862");
      },
      // 覆盖率插桩下 10000 轮 PBKDF2 可能超过默认 1s。
      { timeout: 5000 },
    );
    expect(await screen.findByText("密码修改成功")).toBeInTheDocument();
    await waitFor(() => expect(passwordInput("新密码").value).toBe(""));
    await waitFor(() => {
      expect(
        mockFetch.mock.calls.filter(([url]) => url === "/api/v2/auth/me").length,
      ).toBeGreaterThanOrEqual(2);
    });
  });

  it("初始密码用户隐藏当前密码框并提交 null 旧密码", async () => {
    setup(() => RPC_ON, ME_INITIAL);
    renderPage();
    await screen.findByRole("heading", { name: "用户设置" });
    expect(screen.queryByLabelText("当前密码")).not.toBeInTheDocument();
    fireEvent.change(passwordInput("新密码"), { target: { value: "newpw123" } });
    fireEvent.change(passwordInput("确认新密码"), { target: { value: "newpw123" } });
    fireEvent.click(screen.getByRole("button", { name: "修改密码" }));
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([url]) => url === "/api/v2/auth/password");
      expect(JSON.parse(call![1].body as string).old_password).toBeNull();
    });
  });

  it("两次输入不一致与长度不足就地校验，不发请求", async () => {    setup(() => RPC_ON);
    renderPage();
    await screen.findByRole("heading", { name: "用户设置" });
    fireEvent.change(passwordInput("当前密码"), { target: { value: "oldpw" } });
    fireEvent.change(passwordInput("新密码"), { target: { value: "longenough" } });
    fireEvent.change(passwordInput("确认新密码"), { target: { value: "different" } });
    fireEvent.click(screen.getByRole("button", { name: "修改密码" }));
    expect(await screen.findByText("两次输入的新密码不一致")).toBeInTheDocument();

    fireEvent.change(passwordInput("确认新密码"), { target: { value: "123" } });
    fireEvent.change(passwordInput("新密码"), { target: { value: "123" } });
    fireEvent.click(screen.getByRole("button", { name: "修改密码" }));
    expect(await screen.findByText("新密码长度至少为 6 位")).toBeInTheDocument();
    expect(mockFetch.mock.calls.find(([url]) => url === "/api/v2/auth/password")).toBeUndefined();
  });

  it("服务端拒绝时错误卡片显示后端中文消息", async () => {
    setup((url) => {
      if (url === "/api/v2/auth/password") {
        return json(403, { error: { code: "forbidden", message: "旧密码不正确" } });
      }
      return RPC_ON;
    });
    renderPage();
    await screen.findByRole("heading", { name: "用户设置" });
    fireEvent.change(passwordInput("当前密码"), { target: { value: "bad" } });
    fireEvent.change(passwordInput("新密码"), { target: { value: "newpw123" } });
    fireEvent.change(passwordInput("确认新密码"), { target: { value: "newpw123" } });
    fireEvent.click(screen.getByRole("button", { name: "修改密码" }));
    // 覆盖率插桩下 10000 轮 PBKDF2 可能超过默认 1s。
    expect(await screen.findByText("旧密码不正确", undefined, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.queryByText("密码修改成功")).not.toBeInTheDocument();
  });

  it("用户信息未加载时提示刷新页面并不发请求", async () => {
    setup(() => RPC_ON, { ...ME, username: "" });
    renderPage();
    await screen.findByRole("heading", { name: "用户设置" });
    fireEvent.change(passwordInput("当前密码"), { target: { value: "oldpw" } });
    fireEvent.change(passwordInput("新密码"), { target: { value: "newpw123" } });
    fireEvent.change(passwordInput("确认新密码"), { target: { value: "newpw123" } });
    fireEvent.click(screen.getByRole("button", { name: "修改密码" }));
    expect(await screen.findByText("用户信息未加载，请刷新页面后重试")).toBeInTheDocument();
    expect(mockFetch.mock.calls.find(([url]) => url === "/api/v2/auth/password")).toBeUndefined();
  });
});

describe("ProfilePage 浏览器通知", () => {
  it("浏览器不支持时显示 V1 警告", async () => {
    setup(() => RPC_ON);
    renderPage();
    await screen.findByRole("heading", { name: "用户设置" });
    expect(screen.getByText("您的浏览器不支持通知功能")).toBeInTheDocument();
  });

  it("授权通过后打开总开关并持久化，子开关默认开、可单独关闭", async () => {
    (globalThis as { Notification?: unknown }).Notification = {
      permission: "default",
      requestPermission: jest.fn().mockResolvedValue("granted"),
    };
    setup(() => RPC_ON);
    renderPage();
    await screen.findByRole("heading", { name: "用户设置" });
    const toggle = screen.getByRole("button", { name: "启用通知" });
    expect(toggle.className).toContain("toggle-switch-off");
    fireEvent.click(toggle);
    await waitFor(() => {
      expect(window.localStorage.getItem("notifications.enabled")).toBe("true");
    });
    expect(screen.getByRole("button", { name: "启用通知" }).className).toContain("toggle-switch-on");
    // 子开关出现且默认开启。
    expect(screen.getByText("下载完成时")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "下载完成时通知" }).className).toContain(
      "toggle-switch-on",
    );
    expect(screen.getByRole("button", { name: "下载失败时通知" }).className).toContain(
      "toggle-switch-on",
    );
    fireEvent.click(screen.getByRole("button", { name: "下载完成时通知" }));
    await waitFor(() => {
      expect(window.localStorage.getItem("notifications.on_completed")).toBe("false");
      // 未触碰的子开关不落盘、保持默认开。
      expect(window.localStorage.getItem("notifications.on_failed")).toBeNull();
      expect(screen.getByRole("button", { name: "下载失败时通知" }).className).toContain(
        "toggle-switch-on",
      );
    });
  });

  it("授权被拒时弹 warning 提示且不打开开关", async () => {
    (globalThis as { Notification?: unknown }).Notification = {
      permission: "default",
      requestPermission: jest.fn().mockResolvedValue("denied"),
    };
    setup(() => RPC_ON);
    renderPage();
    await screen.findByRole("heading", { name: "用户设置" });
    fireEvent.click(screen.getByRole("button", { name: "启用通知" }));
    expect(
      await screen.findByText("浏览器通知权限被拒绝，请在浏览器设置中允许通知"),
    ).toBeInTheDocument();
    expect(window.localStorage.getItem("notifications.enabled")).toBeNull();
    expect(screen.getByRole("button", { name: "启用通知" }).className).toContain(
      "toggle-switch-off",
    );
  });

  it("关闭总开关直接持久化 false，不再申请权限", async () => {    window.localStorage.setItem("notifications.enabled", "true");
    const requestPermission = jest.fn();
    (globalThis as { Notification?: unknown }).Notification = {
      permission: "granted",
      requestPermission,
    };
    setup(() => RPC_ON);
    renderPage();
    await screen.findByRole("heading", { name: "用户设置" });
    fireEvent.click(screen.getByRole("button", { name: "启用通知" }));
    await waitFor(() => {
      expect(window.localStorage.getItem("notifications.enabled")).toBe("false");
    });
    expect(requestPermission).not.toHaveBeenCalled();
    expect(screen.queryByText("下载完成时")).not.toBeInTheDocument();
  });

  it("失败子开关可单独关闭并持久化", async () => {
    window.localStorage.setItem("notifications.enabled", "true");
    (globalThis as { Notification?: unknown }).Notification = {
      permission: "granted",
      requestPermission: jest.fn(),
    };
    setup(() => RPC_ON);
    renderPage();
    await screen.findByText("下载完成时");
    fireEvent.click(screen.getByRole("button", { name: "下载失败时通知" }));
    await waitFor(() => {
      expect(window.localStorage.getItem("notifications.on_failed")).toBe("false");
      // 完成子开关未触碰，保持默认开且不落盘。
      expect(window.localStorage.getItem("notifications.on_completed")).toBeNull();
      expect(screen.getByRole("button", { name: "下载完成时通知" }).className).toContain(
        "toggle-switch-on",
      );
      expect(screen.getByRole("button", { name: "下载失败时通知" }).className).toContain(
        "toggle-switch-off",
      );
    });
  });
});

describe("ProfilePage 卸载保护", () => {
  it("卸载后返回的通知授权结果不写本地状态", async () => {
    let release: (value: string) => void = () => undefined;
    (globalThis as { Notification?: unknown }).Notification = {
      permission: "default",
      requestPermission: () =>
        new Promise((resolve) => {
          release = resolve;
        }),
    };
    setup(() => RPC_ON);
    renderPageWithToggle();
    await screen.findByRole("heading", { name: "用户设置" });
    fireEvent.click(screen.getByRole("button", { name: "启用通知" }));
    fireEvent.click(screen.getByRole("button", { name: "卸载页面" }));
    release("granted");
    await act(async () => {
      await Promise.resolve();
    });
    expect(window.localStorage.getItem("notifications.enabled")).toBeNull();
  });

  /** 让挂起请求真正落定后再检查页面没有任何反馈：直接看 DOM 有无新增内容。 */
  async function flush() {
    await act(async () => {
      await new Promise((done) => setTimeout(done, 0));
    });
  }

  /** 剪贴板写入挂起：用于在 onSuccess 回调落到已卸载组件时才释放。 */
  function deferredClipboard() {
    const pending: (() => void)[] = [];
    const writeText = jest.fn(
      () =>
        new Promise<void>((resolve) => {
          pending.push(resolve);
        }),
    );
    return {
      writeText,
      release: () => {
        pending.forEach((resolve) => resolve());
        pending.length = 0;
      },
    };
  }

  /** 只统计「已复制」复位用的 2000ms 定时器，避免混入 Toast 等其他计时器。 */
  function trackResetTimers() {
    const original = window.setTimeout;
    let count = 0;
    const spy = jest.spyOn(window, "setTimeout").mockImplementation(((
      handler: TimerHandler,
      delay?: number,
      ...rest: unknown[]
    ) => {
      if (delay === 2000) count += 1;
      return original(handler, delay, ...rest);
    }) as typeof window.setTimeout);
    return {
      count: () => count,
      restore: () => {
        spy.mockRestore();
      },
    };
  }

  function pendingCall() {
    const control: { resolve: (value: unknown) => void; reject: (error: Error) => void } = {
      resolve: () => undefined,
      reject: () => undefined,
    };
    const promise = new Promise((resolve, reject) => {
      control.resolve = resolve;
      control.reject = reject;
    });
    return { promise, control };
  }

  /**
   * 页面卸载后组件已从 DOM 移除，任何「看不到某段文案」的断言都会恒真。
   * 真实可观测的信号是：catch 块里的 console.error 是否被执行、Toast 宿主是否收到消息。
   */
  function pageErrors(): string[] {
    return consoleError.mock.calls.map((args) => String(args[0]));
  }

  it("卸载后返回的改密失败不再报错到控制台", async () => {
    const { promise, control } = pendingCall();
    setup((url) => (url === "/api/v2/auth/password" ? (promise as never) : RPC_ON));
    renderPageWithToggle();
    await screen.findByRole("heading", { name: "用户设置" });
    fireEvent.change(passwordInput("当前密码"), { target: { value: "oldpw" } });
    fireEvent.change(passwordInput("新密码"), { target: { value: "newpw123" } });
    fireEvent.change(passwordInput("确认新密码"), { target: { value: "newpw123" } });
    fireEvent.click(screen.getByRole("button", { name: "修改密码" }));
    await waitFor(
      () => expect(mockFetch.mock.calls.some(([url]) => url === "/api/v2/auth/password")).toBe(true),
      { timeout: 5000 },
    );

    fireEvent.click(screen.getByRole("button", { name: "卸载页面" }));
    control.reject(new Error("旧密码不正确"));
    await flush();

    // 卸载守卫生效：错误既不会写入已卸载组件，也不会弹成功提示。
    expect(screen.queryByText("密码修改成功")).not.toBeInTheDocument();
    expect(pageErrors()).toEqual([]);
  });

  it("卸载后返回的改密成功不弹成功提示", async () => {
    const { promise, control } = pendingCall();
    setup((url) => (url === "/api/v2/auth/password" ? (promise as never) : RPC_ON));
    renderPageWithToggle();
    await screen.findByRole("heading", { name: "用户设置" });
    fireEvent.change(passwordInput("当前密码"), { target: { value: "oldpw" } });
    fireEvent.change(passwordInput("新密码"), { target: { value: "newpw123" } });
    fireEvent.change(passwordInput("确认新密码"), { target: { value: "newpw123" } });
    fireEvent.click(screen.getByRole("button", { name: "修改密码" }));
    await waitFor(
      () => expect(mockFetch.mock.calls.some(([url]) => url === "/api/v2/auth/password")).toBe(true),
      { timeout: 5000 },
    );

    fireEvent.click(screen.getByRole("button", { name: "卸载页面" }));
    control.resolve(json(200, { ok: true }));
    await flush();

    // Toast 宿主仍在页面上：成功提示若被触发会立刻出现在 DOM 里。
    expect(screen.queryByText("密码修改成功")).not.toBeInTheDocument();
  });

  it("确认框还开着时离开页面，确认后不再发刷新请求", async () => {
    setup(() => RPC_ON);
    renderPageWithToggle();
    await screen.findByText("raw-secret-value");
    fireEvent.click(screen.getByRole("button", { name: "刷新" }));
    const dialog = await screen.findByRole("dialog", { name: "刷新密钥" });

    fireEvent.click(screen.getByRole("button", { name: "卸载页面" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "刷新" }));
    await flush();

    // 页面已卸载：确认框的「刷新」不再触发生效请求。
    expect(
      mockFetch.mock.calls.some(
        ([url, init]) => init?.method === "POST" && url === "/api/v2/credentials/rpc/refresh",
      ),
    ).toBe(false);
  });

  it("卸载后返回的刷新失败不再报错到控制台", async () => {
    const { promise, control } = pendingCall();
    setup((url, init) =>
      init?.method === "POST" && url === "/api/v2/credentials/rpc/refresh" ? (promise as never) : RPC_ON,
    );
    renderPageWithToggle();
    await screen.findByText("raw-secret-value");
    fireEvent.click(screen.getByRole("button", { name: "刷新" }));
    const dialog = await screen.findByRole("dialog", { name: "刷新密钥" });
    fireEvent.click(within(dialog).getByRole("button", { name: "刷新" }));
    await waitFor(() =>
      expect(
        mockFetch.mock.calls.some(
          ([url, init]) => init?.method === "POST" && url === "/api/v2/credentials/rpc/refresh",
        ),
      ).toBe(true),
    );

    fireEvent.click(screen.getByRole("button", { name: "卸载页面" }));
    control.reject(new Error("稍后再试"));
    await flush();

    expect(pageErrors()).toEqual([]);
  });

  it("卸载后返回的刷新成功不改密钥展示", async () => {
    const { promise, control } = pendingCall();
    setup((url, init) =>
      init?.method === "POST" && url === "/api/v2/credentials/rpc/refresh"
        ? (promise as never)
        : RPC_ON,
    );
    renderPageWithToggle();
    await screen.findByText("raw-secret-value");
    fireEvent.click(screen.getByRole("button", { name: "刷新" }));
    const dialog = await screen.findByRole("dialog", { name: "刷新密钥" });
    fireEvent.click(within(dialog).getByRole("button", { name: "刷新" }));
    await waitFor(() =>
      expect(
        mockFetch.mock.calls.some(
          ([url, init]) => init?.method === "POST" && url === "/api/v2/credentials/rpc/refresh",
        ),
      ).toBe(true),
    );

    fireEvent.click(screen.getByRole("button", { name: "卸载页面" }));
    control.resolve({
      enabled: true,
      secret: "brand-new-secret",
    });
    await flush();

    // 旧密钥已随页面卸载消失；新密钥不得凭空出现在页面上。
    expect(screen.queryByText("raw-secret-value")).not.toBeInTheDocument();
    expect(screen.queryByText("brand-new-secret")).not.toBeInTheDocument();
    expect(pageErrors()).toEqual([]);
  });

  it("卸载后返回的 RPC 开关失败不再报错到控制台", async () => {
    const { promise, control } = pendingCall();
    setup((_url, init) => (init?.method === "PUT" ? (promise as never) : RPC_ON));
    renderPageWithToggle();
    await screen.findByText("raw-secret-value");
    fireEvent.click(screen.getByRole("button", { name: "允许外部 aria2 客户端连接" }));
    await waitFor(() => expect(mockFetch.mock.calls.some(([, init]) => init?.method === "PUT")).toBe(true));

    fireEvent.click(screen.getByRole("button", { name: "卸载页面" }));
    control.reject(new Error("状态冲突"));
    await flush();

    expect(pageErrors()).toEqual([]);
  });

  it("卸载后返回的通知拒绝结果不弹警告 Toast", async () => {
    let release: (value: string) => void = () => undefined;
    (globalThis as { Notification?: unknown }).Notification = {
      permission: "default",
      requestPermission: () =>
        new Promise((resolve) => {
          release = resolve;
        }),
    };
    setup(() => RPC_ON);
    renderPageWithToggle();
    await screen.findByRole("heading", { name: "用户设置" });
    fireEvent.click(screen.getByRole("button", { name: "启用通知" }));
    fireEvent.click(screen.getByRole("button", { name: "卸载页面" }));
    release("denied");
    await flush();

    // Toast 宿主仍挂载：警告若被触发就会出现在 DOM 里。
    expect(
      screen.queryByText("浏览器通知权限被拒绝，请在浏览器设置中允许通知"),
    ).not.toBeInTheDocument();
  });

  it("已复制状态下卸载，2 秒回调不再复位状态", async () => {
    const writeText = jest.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    setup(() => RPC_ON);
    renderPageWithToggle();
    await screen.findByText("raw-secret-value");

    jest.useFakeTimers();
    try {
      fireEvent.click(screen.getByRole("button", { name: "复制 RPC 密钥" }));
      await act(async () => {
        await Promise.resolve();
      });
      expect(screen.getAllByRole("button", { name: "已复制" })).toHaveLength(1);

      fireEvent.click(screen.getByRole("button", { name: "卸载页面" }));
      // 卸载后卸载清理会 clearTimeout；这里再手动推进时间，确认回调守卫也不写状态。
      act(() => {
        jest.advanceTimersByTime(2000);
      });
      expect(screen.queryAllByRole("button", { name: "已复制" })).toHaveLength(0);
      expect(pageErrors()).toEqual([]);
    } finally {
      jest.useRealTimers();
    }
  });

  it("已复制地址状态下卸载，2 秒回调不再复位状态", async () => {
    const writeText = jest.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    setup(() => RPC_ON);
    renderPageWithToggle();
    await screen.findByText("raw-secret-value");

    jest.useFakeTimers();
    try {
      fireEvent.click(screen.getByRole("button", { name: "复制 RPC 地址" }));
      await act(async () => {
        await Promise.resolve();
      });
      expect(writeText).toHaveBeenLastCalledWith(RPC_URL);

      fireEvent.click(screen.getByRole("button", { name: "卸载页面" }));
      act(() => {
        jest.advanceTimersByTime(2000);
      });
      expect(screen.queryAllByRole("button", { name: "已复制" })).toHaveLength(0);
      expect(pageErrors()).toEqual([]);
    } finally {
      jest.useRealTimers();
    }
  });

  it("通知子开关变更在卸载后被忽略", async () => {
    window.localStorage.setItem("notifications.enabled", "true");
    (globalThis as { Notification?: unknown }).Notification = {
      permission: "granted",
      requestPermission: jest.fn(),
    };
    setup(() => RPC_ON);
    renderPageWithToggle();
    await screen.findByText("下载完成时");

    const completeToggle = screen.getByRole("button", { name: "下载完成时通知" });
    fireEvent.click(screen.getByRole("button", { name: "卸载页面" }));
    // 卸载后旧节点上的事件仍可能被触发（React 会忽略），关键是不落盘。
    fireEvent.click(completeToggle);
    await flush();

    expect(window.localStorage.getItem("notifications.on_completed")).toBeNull();
  });

  it("通知子开关在挂载时正常落盘（对照组）", async () => {
    window.localStorage.setItem("notifications.enabled", "true");
    (globalThis as { Notification?: unknown }).Notification = {
      permission: "granted",
      requestPermission: jest.fn(),
    };
    setup(() => RPC_ON);
    renderPage();
    await screen.findByText("下载完成时");

    fireEvent.click(screen.getByRole("button", { name: "下载完成时通知" }));
    await waitFor(() => {
      expect(window.localStorage.getItem("notifications.on_completed")).toBe("false");
    });
  });

  it("复制密钥成功后卸载，不再启动 2 秒复位计时器", async () => {
    const clipboard = deferredClipboard();
    Object.assign(navigator, { clipboard: { writeText: clipboard.writeText } });
    const resetTimers = trackResetTimers();
    try {
      setup(() => RPC_ON);
      renderPageWithToggle();
      await screen.findByText("raw-secret-value");

      fireEvent.click(screen.getByRole("button", { name: "复制 RPC 密钥" }));
      expect(clipboard.writeText).toHaveBeenCalledWith("raw-secret-value");

      fireEvent.click(screen.getByRole("button", { name: "卸载页面" }));
      clipboard.release();
      await flush();

      // 卸载守卫生效：不再为「已复制」文案安排 2 秒复位计时器。
      expect(resetTimers.count()).toBe(0);
    } finally {
      resetTimers.restore();
    }
  });

  it("复制 RPC 地址成功后卸载，不再启动 2 秒复位计时器", async () => {
    const clipboard = deferredClipboard();
    Object.assign(navigator, { clipboard: { writeText: clipboard.writeText } });
    const resetTimers = trackResetTimers();
    try {
      setup(() => RPC_ON);
      renderPageWithToggle();
      await screen.findByText("raw-secret-value");

      // 第二个「复制」按钮对应 RPC 地址。
      fireEvent.click(screen.getByRole("button", { name: "复制 RPC 地址" }));
      expect(clipboard.writeText).toHaveBeenCalledWith(RPC_URL);

      fireEvent.click(screen.getByRole("button", { name: "卸载页面" }));
      clipboard.release();
      await flush();

      expect(resetTimers.count()).toBe(0);
    } finally {
      resetTimers.restore();
    }
  });

  it("复制后卸载，2 秒计时器不写已复制状态", async () => {
    const writeText = jest.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    setup(() => RPC_ON);
    renderPageWithToggle();
    await screen.findByText("raw-secret-value");
    jest.useFakeTimers();
    try {
      fireEvent.click(screen.getByRole("button", { name: "复制 RPC 密钥" }));
      await act(async () => {
        await Promise.resolve();
      });
      expect(screen.getAllByRole("button", { name: "已复制" })).toHaveLength(1);
      fireEvent.click(screen.getByRole("button", { name: "卸载页面" }));
      act(() => {
        jest.advanceTimersByTime(2000);
      });
      expect(screen.queryAllByRole("button", { name: "已复制" })).toHaveLength(0);
    } finally {
      jest.useRealTimers();
    }
  });
});

describe("ProfilePage 外部访问", () => {
  it("开启时展示 RPC 密钥与地址（V1 文案）", async () => {
    setup(() => RPC_ON);
    renderPage();
    await screen.findByRole("heading", { name: "用户设置" });
    expect(screen.getByText("允许外部 aria2 客户端连接")).toBeInTheDocument();
    expect(screen.getByText("开启后可使用 AriaNg、Motrix 等客户端管理当前用户的下载任务。")).toBeInTheDocument();
    expect(screen.getByText("raw-secret-value")).toBeInTheDocument();
    expect(screen.getByText(RPC_URL)).toBeInTheDocument();
  });

  it("关闭 RPC 走 PUT，密钥区块随之隐藏", async () => {
    setup((_url, init) => {
      if (init?.method === "PUT") return RPC_OFF;
      return RPC_ON;
    });
    renderPage();
    await screen.findByText("raw-secret-value");
    fireEvent.click(screen.getByRole("button", { name: "允许外部 aria2 客户端连接" }));
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([, i]) => i?.method === "PUT");
      expect(JSON.parse(call![1].body as string)).toEqual({ enabled: false });
    });
    await waitFor(() => expect(screen.queryByText("raw-secret-value")).not.toBeInTheDocument());
  });

  it("从关闭状态开启 RPC，返回新密钥并展示", async () => {
    setup((_url, init) => {
      if (init?.method === "PUT") return RPC_ON;
      return RPC_OFF;
    });
    renderPage();
    await screen.findByRole("heading", { name: "用户设置" });
    expect(screen.queryByText("raw-secret-value")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "允许外部 aria2 客户端连接" }));
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([, i]) => i?.method === "PUT");
      expect(JSON.parse(call![1].body as string)).toEqual({ enabled: true });
    });
    expect(await screen.findByText("raw-secret-value")).toBeInTheDocument();
  });

  it("刷新密钥需经危险确认框，取消不发请求", async () => {
    setup((_url, init) => {
      if (init?.method === "POST" && _url === "/api/v2/credentials/rpc/refresh") {
        return { enabled: true, secret: "brand-new-secret" };
      }
      return RPC_ON;
    });
    renderPage();
    await screen.findByText("raw-secret-value");
    fireEvent.click(screen.getByRole("button", { name: "刷新" }));
    const dialog = await screen.findByRole("dialog", { name: "刷新密钥" });
    expect(dialog.textContent).toContain("刷新后旧的密钥将立即失效，确定继续？");
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    expect(
      mockFetch.mock.calls.find(
        ([url, i]) => i?.method === "POST" && url === "/api/v2/credentials/rpc/refresh",
      ),
    ).toBeUndefined();

    fireEvent.click(screen.getByRole("button", { name: "刷新" }));
    const dialog2 = await screen.findByRole("dialog", { name: "刷新密钥" });
    fireEvent.click(within(dialog2).getByRole("button", { name: "刷新" }));
    await waitFor(() => {
      expect(
        mockFetch.mock.calls.find(
          ([url, i]) => i?.method === "POST" && url === "/api/v2/credentials/rpc/refresh",
        ),
      ).toBeDefined();
    });
    expect(await screen.findByText("brand-new-secret")).toBeInTheDocument();
  });

  it("刷新失败在错误卡片显示原因", async () => {
    setup((_url, init) => {
      if (init?.method === "POST" && _url === "/api/v2/credentials/rpc/refresh") {
        return json(429, { error: { code: "rate_limited", message: "稍后再试" } });
      }
      return RPC_ON;
    });
    renderPage();
    await screen.findByText("raw-secret-value");
    fireEvent.click(screen.getByRole("button", { name: "刷新" }));
    const dialog = await screen.findByRole("dialog", { name: "刷新密钥" });
    fireEvent.click(within(dialog).getByRole("button", { name: "刷新" }));
    expect(await screen.findByText("刷新 Secret 失败: 稍后再试")).toBeInTheDocument();
  });

  it("开关失败在错误卡片显示原因", async () => {
    setup((_url, init) => {
      if (init?.method === "PUT") {
        return json(409, { error: { code: "conflict", message: "状态冲突" } });
      }
      return RPC_ON;
    });
    renderPage();
    await screen.findByText("raw-secret-value");
    fireEvent.click(screen.getByRole("button", { name: "允许外部 aria2 客户端连接" }));
    expect(await screen.findByText("设置 RPC 访问失败: 状态冲突")).toBeInTheDocument();
  });

  it("RPC 首读失败不阻塞页面渲染", async () => {
    const consoleError = jest.spyOn(console, "error").mockImplementation(() => undefined);
    setup((url) => {
      if (url === "/api/v2/credentials/rpc") {
        return json(500, { error: { code: "internal", message: "后端错误" } });
      }
      return RPC_ON;
    });
    renderPage();
    expect(await screen.findByRole("heading", { name: "用户设置" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "修改密码" })).toBeInTheDocument();
    consoleError.mockRestore();
  });

  it("复制密钥与地址调用剪贴板，按钮短暂变为「已复制」", async () => {
    const writeText = jest.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    setup(() => RPC_ON);
    renderPage();
    await screen.findByText("raw-secret-value");
    expect(screen.getByRole("button", { name: "复制 RPC 密钥" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "复制 RPC 地址" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "复制 RPC 密钥" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("raw-secret-value"));
    // 密钥按钮文案切为「已复制」；只剩地址的「复制」按钮。
    await waitFor(() =>
      expect(screen.getAllByRole("button", { name: "已复制" })).toHaveLength(1),
    );
    expect(screen.queryByRole("button", { name: "复制 RPC 密钥" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "复制 RPC 地址" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "复制 RPC 地址" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(RPC_URL));
    await waitFor(() =>
      expect(screen.getAllByRole("button", { name: "已复制" })).toHaveLength(2),
    );
  });

  it("「已复制」文案在 2 秒后恢复为「复制」", async () => {
    const writeText = jest.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    setup(() => RPC_ON);
    renderPage();
    await screen.findByText("raw-secret-value");
    jest.useFakeTimers();
    try {
      // 密钥复制：按钮短暂变「已复制」后复位。
      fireEvent.click(screen.getByRole("button", { name: "复制 RPC 密钥" }));
      await act(async () => {
        await Promise.resolve();
      });
      expect(screen.getAllByRole("button", { name: "已复制" })).toHaveLength(1);
      act(() => {
        jest.advanceTimersByTime(2000);
      });
      expect(screen.queryAllByRole("button", { name: "已复制" })).toHaveLength(0);
      expect(screen.getByRole("button", { name: "复制 RPC 密钥" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "复制 RPC 地址" })).toBeInTheDocument();

      // 地址复制：同样 2 秒后复位。
      fireEvent.click(screen.getByRole("button", { name: "复制 RPC 地址" }));
      await act(async () => {
        await Promise.resolve();
      });
      expect(writeText).toHaveBeenLastCalledWith(RPC_URL);
      expect(screen.getAllByRole("button", { name: "已复制" })).toHaveLength(1);
      act(() => {
        jest.advanceTimersByTime(2000);
      });
      expect(screen.queryAllByRole("button", { name: "已复制" })).toHaveLength(0);
    } finally {
      jest.useRealTimers();
    }
  });
});
