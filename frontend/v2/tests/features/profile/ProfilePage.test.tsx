import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { ProfilePage } from "@/features/profile/ProfilePage";
import type { RpcAccessResponse, UserResponse } from "@/shared/api/types";

const mockFetch = jest.fn();
global.fetch = mockFetch as unknown as typeof fetch;

const ME: UserResponse = {
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

const RPC_ON: RpcAccessResponse = { enabled: true, secret_prefix: "abcd12", secret: null };

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
    const result = handler(url, init);
    if (
      typeof result === "object" &&
      result !== null &&
      typeof (result as { json?: unknown }).json === "function"
    ) {
      return result;
    }
    // 处理器未命中时的默认响应：RPC 状态读取。
    if (url === "/api/v2/credentials/rpc" && init?.method === "GET" && result === null) {
      return json(200, RPC_ON);
    }
    return json(200, result);
  });
  return render(
    <MemoryRouter initialEntries={["/profile"]}>
      <AppProviders>
        <ProfilePage />
      </AppProviders>
    </MemoryRouter>,
  );
}

function passwordInput(label: string): HTMLInputElement {
  return screen.getByLabelText(label) as HTMLInputElement;
}

beforeEach(() => {
  window.localStorage.clear();
  delete (globalThis as { Notification?: unknown }).Notification;
});

afterEach(() => {
  delete (globalThis as { Notification?: unknown }).Notification;
});

describe("ProfilePage password", () => {
  it("changes password with hashed payloads and clears the form", async () => {
    setup((url) => {
      if (url === "/api/v2/auth/password") return { ok: true };
      return RPC_ON;
    });
    await screen.findByText("RPC 访问");
    fireEvent.change(passwordInput("旧密码"), { target: { value: "oldpw" } });
    fireEvent.change(passwordInput("新密码"), { target: { value: "newpw123" } });
    fireEvent.change(passwordInput("确认新密码"), { target: { value: "newpw123" } });
    fireEvent.click(screen.getByRole("button", { name: "修改密码" }));
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([url]) => url === "/api/v2/auth/password");
      expect(call).toBeDefined();
      const body = JSON.parse(call![1].body as string);
      // 独立基准（username=alice）：pbkdf2_hmac('sha256', pw, sha256(b'alice').hexdigest().encode(), 10000).hex()
      expect(body.old_password).toBe("d8b20a1f89e56c1cde5b162e3b10ed180b01d5711a8cbf29e2b1018ac7b35d01");
      expect(body.new_password).toBe("253af49750a3f4cd54a44ced05695cdc4fd34e9d9461dccd4d5a5fd08815d862");
    });
    expect(await screen.findByText("密码修改成功")).toBeInTheDocument();
    await waitFor(() => expect(passwordInput("新密码").value).toBe("")); // 表单已清空。
    // 修改成功后重读当前用户。
    await waitFor(() => {
      expect(mockFetch.mock.calls.filter(([url]) => url === "/api/v2/auth/me").length).toBeGreaterThanOrEqual(2);
    });
  });

  it("hides old password field and shows banner for initial-password users", async () => {
    setup(() => RPC_ON, { ...ME, is_initial_password: true });
    expect(await screen.findByText(/使用初始密码/)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByLabelText("旧密码")).not.toBeInTheDocument());
    fireEvent.change(passwordInput("新密码"), { target: { value: "newpw123" } });
    fireEvent.change(passwordInput("确认新密码"), { target: { value: "newpw123" } });
    fireEvent.click(screen.getByRole("button", { name: "修改密码" }));
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([url]) => url === "/api/v2/auth/password");
      expect(JSON.parse(call![1].body as string).old_password).toBeNull();
    });
  });

  it("validates short and mismatched new passwords inline without submitting", async () => {
    setup(() => RPC_ON);
    await screen.findByText("RPC 访问");
    fireEvent.change(passwordInput("新密码"), { target: { value: "123" } });
    fireEvent.change(passwordInput("确认新密码"), { target: { value: "123" } });
    fireEvent.click(screen.getByRole("button", { name: "修改密码" }));
    expect(await screen.findByText("新密码长度至少 6 位")).toBeInTheDocument();

    fireEvent.change(passwordInput("新密码"), { target: { value: "longenough" } });
    fireEvent.change(passwordInput("确认新密码"), { target: { value: "different" } });
    fireEvent.click(screen.getByRole("button", { name: "修改密码" }));
    expect(await screen.findByText("两次输入的新密码不一致")).toBeInTheDocument();
    expect(mockFetch.mock.calls.find(([url]) => url === "/api/v2/auth/password")).toBeUndefined();
  });

  it("shows server error inline when old password is wrong", async () => {
    setup((url) => {
      if (url === "/api/v2/auth/password") {
        return json(403, { error: { code: "forbidden", message: "旧密码不正确" } });
      }
      return RPC_ON;
    });
    await screen.findByText("RPC 访问");
    fireEvent.change(passwordInput("旧密码"), { target: { value: "bad" } });
    fireEvent.change(passwordInput("新密码"), { target: { value: "newpw123" } });
    fireEvent.change(passwordInput("确认新密码"), { target: { value: "newpw123" } });
    fireEvent.click(screen.getByRole("button", { name: "修改密码" }));
    expect(await screen.findByText("旧密码不正确")).toBeInTheDocument();
    expect(screen.queryByText("密码修改成功")).not.toBeInTheDocument();
  });
});

describe("ProfilePage notifications", () => {
  it("requests permission and persists the enabled flag", async () => {
    (globalThis as { Notification?: unknown }).Notification = {
      permission: "default",
      requestPermission: jest.fn().mockResolvedValue("granted"),
    };
    setup(() => RPC_ON);
    await screen.findByText("RPC 访问");
    fireEvent.click(screen.getByLabelText("启用桌面通知"));
    await waitFor(() => {
      expect(window.localStorage.getItem("notifications.enabled")).toBe("true");
    });
  });

  it("keeps the toggle off with a Chinese toast when permission is denied", async () => {
    (globalThis as { Notification?: unknown }).Notification = {
      permission: "default",
      requestPermission: jest.fn().mockResolvedValue("denied"),
    };
    setup(() => RPC_ON);
    await screen.findByText("RPC 访问");
    fireEvent.click(screen.getByLabelText("启用桌面通知"));
    expect(await screen.findByText(/通知权限被拒绝/)).toBeInTheDocument();
    expect(window.localStorage.getItem("notifications.enabled")).toBeNull();
    expect(screen.getByText(/重新允许/)).toBeInTheDocument();
  });

  it("disables the toggle and explains when Notification is unsupported", async () => {
    setup(() => RPC_ON);
    await screen.findByText("RPC 访问");
    const toggle = screen.getByLabelText("启用桌面通知") as HTMLInputElement;
    expect(toggle.disabled).toBe(true);
    expect(screen.getByText("当前浏览器不支持桌面通知。")).toBeInTheDocument();
  });

  it("turning the toggle off persists false without permission request", async () => {
    window.localStorage.setItem("notifications.enabled", "true");
    const requestPermission = jest.fn();
    (globalThis as { Notification?: unknown }).Notification = {
      permission: "granted",
      requestPermission,
    };
    setup(() => RPC_ON);
    await screen.findByText("RPC 访问");
    fireEvent.click(screen.getByLabelText("启用桌面通知"));
    await waitFor(() => {
      expect(window.localStorage.getItem("notifications.enabled")).toBe("false");
    });
    expect(requestPermission).not.toHaveBeenCalled();
  });

  it("treats a throwing requestPermission as denied", async () => {
    (globalThis as { Notification?: unknown }).Notification = {
      permission: "default",
      requestPermission: jest.fn().mockRejectedValue(new Error("boom")),
    };
    setup(() => RPC_ON);
    await screen.findByText("RPC 访问");
    fireEvent.click(screen.getByLabelText("启用桌面通知"));
    const host = await screen.findByRole("status");
    await waitFor(() => {
      expect(host.textContent).toContain("浏览器通知权限被拒绝");
    });
    expect(window.localStorage.getItem("notifications.enabled")).toBeNull();
  });

  // ============ G3 通知子开关 ============

  it("sub-switches default on and persist when toggled", async () => {
    (globalThis as { Notification?: unknown }).Notification = {
      permission: "granted",
      requestPermission: jest.fn(),
    };
    window.localStorage.setItem("notifications.enabled", "true");
    setup(() => RPC_ON);
    await screen.findByText("RPC 访问");
    // 默认开启。
    expect(screen.getByLabelText("下载完成时通知")).toBeChecked();
    expect(screen.getByLabelText("下载失败时通知")).toBeChecked();
    fireEvent.click(screen.getByLabelText("下载完成时通知"));
    await waitFor(() => {
      expect(window.localStorage.getItem("notifications.on_completed")).toBe("false");
      // 未触碰的子开关保持默认开启（且仅在变更时才落盘）。
      expect(window.localStorage.getItem("notifications.on_failed")).toBeNull();
      expect(screen.getByLabelText("下载失败时通知")).toBeChecked();
    });
  });

  it("sub-switches are disabled while the master switch is off", async () => {
    setup(() => RPC_ON);
    await screen.findByText("RPC 访问");
    expect(screen.getByLabelText("下载完成时通知")).toBeDisabled();
    expect(screen.getByLabelText("下载失败时通知")).toBeDisabled();
  });
});

describe("ProfilePage RPC access", () => {
  it("shows the endpoint URL with copy button", async () => {
    const writeText = jest.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    setup(() => RPC_ON);
    await screen.findByText("RPC 访问");
    expect(screen.getByText(`${window.location.origin}/aria2/jsonrpc`)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "复制" }));
    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith(`${window.location.origin}/aria2/jsonrpc`),
    );
    expect(await screen.findByText("已复制RPC 地址")).toBeInTheDocument();
  });

  it("toasts a Chinese fallback when clipboard copy fails", async () => {
    Object.assign(navigator, { clipboard: { writeText: jest.fn().mockRejectedValue(new Error("denied")) } });
    setup(() => RPC_ON);
    await screen.findByText("RPC 访问");
    fireEvent.click(screen.getByRole("button", { name: "复制" }));
    expect(await screen.findByText("复制失败，请手动复制")).toBeInTheDocument();
  });

  it("keeps the password form usable when the RPC first read fails", async () => {
    setup((url) => {
      if (url === "/api/v2/credentials/rpc") {
        return json(500, { error: { code: "internal", message: "后端错误" } });
      }
      return RPC_ON;
    });
    expect(await screen.findByText("后端错误")).toBeInTheDocument();
    expect(screen.getByLabelText("新密码")).toBeInTheDocument(); // 改密区不受阻塞。
  });

  it("toggles RPC access off via PUT and reflects the state", async () => {
    setup((_url, init) => {
      if (init?.method === "PUT") return { enabled: false, secret_prefix: null, secret: null };
      return RPC_ON;
    });
    await screen.findByText("已开启");
    fireEvent.click(screen.getByLabelText("开启 RPC 访问"));
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([, init2]) => init2?.method === "PUT");
      expect(JSON.parse(call![1].body as string)).toEqual({ enabled: false });
    });
    expect(await screen.findByText("已关闭")).toBeInTheDocument();
  });

  it("surfaces RPC toggle failure inline", async () => {
    setup((_url, init) => {
      if (init?.method === "PUT") {
        return json(429, { error: { code: "rate_limited", message: "请求过于频繁" } });
      }
      return RPC_ON;
    });
    await screen.findByText("已开启");
    fireEvent.click(screen.getByLabelText("开启 RPC 访问"));
    expect(await screen.findByText("请求过于频繁")).toBeInTheDocument();
  });

  it("refreshes the secret only after danger confirmation and reveals it once", async () => {
    const writeText = jest.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    setup((_url, init) => {
      if (init?.method === "POST" && _url === "/api/v2/credentials/rpc/refresh") {
        return { enabled: true, secret_prefix: "newpfx", secret: "raw-secret-once" };
      }
      return RPC_ON;
    });
    await screen.findByText("已开启");
    fireEvent.click(screen.getByRole("button", { name: "刷新 Secret" }));
    const dialog = await screen.findByRole("dialog", { name: "刷新 RPC Secret" });
    expect(dialog.textContent).toContain("立即失效");
    fireEvent.click(screen.getByRole("button", { name: "取消" })); // 取消不请求。
    expect(
      mockFetch.mock.calls.find(([url, init2]) => init2?.method === "POST" && url === "/api/v2/credentials/rpc/refresh"),
    ).toBeUndefined();

    fireEvent.click(screen.getByRole("button", { name: "刷新 Secret" }));
    await screen.findByRole("dialog", { name: "刷新 RPC Secret" });
    fireEvent.click(screen.getAllByRole("button", { name: "刷新" }).at(-1)!);
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(
        ([url, init2]) => init2?.method === "POST" && url === "/api/v2/credentials/rpc/refresh",
      );
      expect(call).toBeDefined();
    });
    expect(await screen.findByText("raw-secret-once")).toBeInTheDocument();
    expect(await screen.findByText("新 Secret（仅本次显示，请立即保存）：")).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("button", { name: "复制" }).at(-1)!);
    await waitFor(() => expect(writeText).toHaveBeenLastCalledWith("raw-secret-once"));
  });

  it("disables the refresh button while RPC is off", async () => {
    setup(() => ({ enabled: false, secret_prefix: null, secret: null }));
    await screen.findByText("已关闭");
    expect(
      (screen.getByRole("button", { name: "刷新 Secret" }) as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("masks the secret prefix while RPC is disabled", async () => {
    setup(() => ({ enabled: false, secret_prefix: "ab12cd", secret: null }));
    await screen.findByText("已关闭");
    expect(screen.getByText("••••••")).toBeInTheDocument();
    expect(screen.queryByText("ab12cd")).not.toBeInTheDocument();
  });

  it("enables RPC from an off state via PUT", async () => {
    setup((_url, init) => {
      if (init?.method === "PUT") return RPC_ON;
      return { enabled: false, secret_prefix: null, secret: null };
    });
    await screen.findByText("已关闭");
    fireEvent.click(screen.getByLabelText("开启 RPC 访问"));
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([, init2]) => init2?.method === "PUT");
      expect(JSON.parse(call![1].body as string)).toEqual({ enabled: true });
    });
    expect(await screen.findByText("RPC 访问已开启")).toBeInTheDocument();
    expect(await screen.findByText("已开启")).toBeInTheDocument();
  });

  it("does not reveal a block when the refresh response has no secret", async () => {
    setup((_url, init) => {
      if (init?.method === "POST" && _url === "/api/v2/credentials/rpc/refresh") {
        return { enabled: true, secret_prefix: "newpfx", secret: null };
      }
      return RPC_ON;
    });
    await screen.findByText("已开启");
    fireEvent.click(screen.getByRole("button", { name: "刷新 Secret" }));
    await screen.findByRole("dialog", { name: "刷新 RPC Secret" });
    fireEvent.click(screen.getAllByRole("button", { name: "刷新" }).at(-1)!);
    expect(await screen.findByText("RPC Secret 已刷新")).toBeInTheDocument();
    expect(screen.queryByText(/仅本次显示/)).not.toBeInTheDocument();
  });

  it("ignores a second submit while a password change is pending", async () => {
    let release: (value: unknown) => void = () => undefined;
    const pending = new Promise((resolve) => {
      release = resolve;
    });
    setup((url) => {
      if (url === "/api/v2/auth/password") return pending as never;
      return RPC_ON;
    });
    await screen.findByText("RPC 访问");
    fireEvent.change(passwordInput("旧密码"), { target: { value: "oldpw" } });
    fireEvent.change(passwordInput("新密码"), { target: { value: "newpw123" } });
    fireEvent.change(passwordInput("确认新密码"), { target: { value: "newpw123" } });
    fireEvent.click(screen.getByRole("button", { name: "修改密码" }));
    await waitFor(() => {
      expect(
        mockFetch.mock.calls.filter(([url]) => url === "/api/v2/auth/password").length,
      ).toBe(1);
    });
    fireEvent.click(screen.getByRole("button", { name: "提交中…" })); // 提交中重复点击。
    release({ ok: true });
    await waitFor(() => {
      expect(
        mockFetch.mock.calls.filter(([url]) => url === "/api/v2/auth/password").length,
      ).toBe(1);
    });
  });

  it("shows inline error when refresh fails", async () => {
    setup((_url, init) => {
      if (init?.method === "POST" && _url === "/api/v2/credentials/rpc/refresh") {
        return json(429, { error: { code: "rate_limited", message: "稍后再试" } });
      }
      return RPC_ON;
    });
    await screen.findByText("已开启");
    fireEvent.click(screen.getByRole("button", { name: "刷新 Secret" }));
    await screen.findByRole("dialog", { name: "刷新 RPC Secret" });
    fireEvent.click(screen.getAllByRole("button", { name: "刷新" }).at(-1)!);
    expect(await screen.findByText("稍后再试")).toBeInTheDocument();
  });
});
