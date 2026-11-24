import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import ProfilePage from "@/app/(authenticated)/profile/page";
import { api } from "@/lib/api";
import {
  getNotificationSettings,
  saveNotificationSettings,
  requestNotificationPermission,
} from "@/lib/notification";

const showToastMock = jest.fn();
const showConfirmMock = jest.fn();
const refreshUserMock = jest.fn();
const authState: {
  user: {
    id: number;
    username: string;
    is_admin: boolean;
    quota: number;
    is_initial_password: boolean;
  } | null;
} = {
  user: {
    id: 1,
    username: "admin",
    is_admin: true,
    quota: 1024 * 1024 * 1024,
    is_initial_password: false,
  },
};

jest.mock("@/components/Toast", () => ({
  __esModule: true,
  useToast: () => ({
    showToast: showToastMock,
    showConfirm: showConfirmMock,
  }),
}));

jest.mock("@/lib/AuthContext", () => ({
  __esModule: true,
  useAuth: () => ({
    user: authState.user,
    refreshUser: refreshUserMock,
  }),
}));

jest.mock("@/lib/notification", () => ({
  __esModule: true,
  getNotificationSettings: jest.fn(() => ({ enabled: false, onComplete: true, onError: true })),
  saveNotificationSettings: jest.fn(),
  requestNotificationPermission: jest.fn().mockResolvedValue(true),
}));

jest.mock("@/lib/api", () => ({
  __esModule: true,
  api: {
    getRpcAccess: jest.fn(),
    setRpcAccess: jest.fn(),
    refreshRpcSecret: jest.fn(),
    changePassword: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;
const mockSaveNotificationSettings = saveNotificationSettings as jest.Mocked<
  typeof saveNotificationSettings
>;
const mockRequestPermission = requestNotificationPermission as jest.MockedFunction<
  typeof requestNotificationPermission
>;

const disabledRpcAccess = { enabled: false, secret: null, created_at: null };
const enabledRpcAccess = {
  enabled: true,
  secret: "rpc-secret",
  created_at: "2024-01-01T00:00:00Z",
};

async function renderReady() {
  render(<ProfilePage />);
  await screen.findByRole("heading", { name: "用户设置" });
}

describe("ProfilePage", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    window.history.pushState({}, "", "/profile");
    authState.user = {
      id: 1,
      username: "admin",
      is_admin: true,
      quota: 1024 * 1024 * 1024,
      is_initial_password: false,
    };
    Object.assign(navigator, {
      clipboard: {
        writeText: jest.fn().mockResolvedValue(undefined),
      },
    });
    showConfirmMock.mockResolvedValue(false);
    (globalThis as { Notification?: unknown }).Notification = class MockNotification {};
    mockApi.getRpcAccess.mockResolvedValue(disabledRpcAccess as never);
    mockApi.setRpcAccess.mockResolvedValue(disabledRpcAccess as never);
    mockApi.refreshRpcSecret.mockResolvedValue(enabledRpcAccess as never);
    mockApi.changePassword.mockResolvedValue({ ok: true, message: "ok" } as never);
    mockRequestPermission.mockResolvedValue(true);
  });

  afterEach(() => {
    delete (globalThis as { Notification?: unknown }).Notification;
  });

  test("renders profile settings", async () => {
    render(<ProfilePage />);

    expect(await screen.findByRole("heading", { name: "用户设置" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "修改密码", level: 2 })).toBeInTheDocument();
    expect(mockApi.getRpcAccess).toHaveBeenCalled();
  });

  test("renders without crashing when rpc access state fails to load", async () => {
    const errorSpy = jest.spyOn(console, "error").mockImplementation(() => {});
    mockApi.getRpcAccess.mockRejectedValue(new Error("rpc down") as never);

    await renderReady();

    expect(errorSpy).toHaveBeenCalledWith(
      "加载 RPC 访问状态失败",
      expect.any(Error),
    );
    expect(screen.getByRole("heading", { name: "外部访问", level: 2 })).toBeInTheDocument();
    errorSpy.mockRestore();
  });

  test.each([
    {
      name: "new passwords do not match",
      newPassword: "new-pass-1",
      confirmPassword: "new-pass-2",
      message: "两次输入的新密码不一致",
    },
    {
      name: "new password is too short",
      newPassword: "123",
      confirmPassword: "123",
      message: "新密码长度至少为 6 位",
    },
  ])("shows validation error when $name", async ({ newPassword, confirmPassword, message }) => {
    const { container } = render(<ProfilePage />);

    expect(await screen.findByText("用户设置")).toBeInTheDocument();
    const passwordInputs = container.querySelectorAll("input[type='password']");
    fireEvent.change(passwordInputs[0], {
      target: { value: "old-pass" },
    });
    fireEvent.change(passwordInputs[1], {
      target: { value: newPassword },
    });
    fireEvent.change(passwordInputs[2], {
      target: { value: confirmPassword },
    });
    fireEvent.click(screen.getByRole("button", { name: "修改密码" }));

    await waitFor(() => {
      expect(screen.getByText(message)).toBeInTheDocument();
    });
  });

  test("changes password successfully and resets the form", async () => {
    const { container } = await (async () => {
      const result = render(<ProfilePage />);
      await screen.findByText("用户设置");
      return result;
    })();

    const passwordInputs = container.querySelectorAll("input[type='password']");
    fireEvent.change(passwordInputs[0], { target: { value: "old-pass" } });
    fireEvent.change(passwordInputs[1], { target: { value: "new-pass-1" } });
    fireEvent.change(passwordInputs[2], { target: { value: "new-pass-1" } });
    fireEvent.click(screen.getByRole("button", { name: "修改密码" }));

    await waitFor(() => {
      expect(mockApi.changePassword).toHaveBeenCalledWith("old-pass", "new-pass-1", "admin");
    });
    expect(showToastMock).toHaveBeenCalledWith("密码修改成功", "success");
    expect(refreshUserMock).toHaveBeenCalled();
    passwordInputs.forEach((input) => {
      expect(input).toHaveValue("");
    });
  });

  test.each([
    {
      name: "structured detail error",
      error: new Error(JSON.stringify({ detail: "旧密码错误" })),
      expected: "旧密码错误",
    },
    {
      name: "plain text error",
      error: new Error("服务暂不可用"),
      expected: "服务暂不可用",
    },
  ])("shows api error message when change password fails ($name)", async ({ error, expected }) => {
    mockApi.changePassword.mockRejectedValue(error as never);
    const { container } = render(<ProfilePage />);

    expect(await screen.findByText("用户设置")).toBeInTheDocument();
    const passwordInputs = container.querySelectorAll("input[type='password']");
    fireEvent.change(passwordInputs[0], { target: { value: "old-pass" } });
    fireEvent.change(passwordInputs[1], { target: { value: "new-pass-1" } });
    fireEvent.change(passwordInputs[2], { target: { value: "new-pass-1" } });
    fireEvent.click(screen.getByRole("button", { name: "修改密码" }));

    expect(await screen.findByText(expected)).toBeInTheDocument();
    expect(showToastMock).not.toHaveBeenCalledWith("密码修改成功", "success");
  });

  test("shows error instead of crashing when user info is missing on password submit", async () => {
    authState.user = null;
    const { container } = render(<ProfilePage />);
    expect(await screen.findByText("用户设置")).toBeInTheDocument();

    const passwordInputs = container.querySelectorAll("input[type='password']");
    fireEvent.change(passwordInputs[0], {
      target: { value: "old-pass" },
    });
    fireEvent.change(passwordInputs[1], {
      target: { value: "new-pass-1" },
    });
    fireEvent.change(passwordInputs[2], {
      target: { value: "new-pass-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "修改密码" }));

    await waitFor(() => {
      expect(screen.getByText("用户信息未加载，请刷新页面后重试")).toBeInTheDocument();
    });
    expect(mockApi.changePassword).not.toHaveBeenCalled();
  });

  test("shows unsupported notice when browser lacks Notification support", async () => {
    delete (globalThis as { Notification?: unknown }).Notification;

    await renderReady();

    expect(screen.getByText("您的浏览器不支持通知功能")).toBeInTheDocument();
  });

  test.each([
    {
      name: "granted",
      granted: true,
      expectEnabled: true,
    },
    {
      name: "denied",
      granted: false,
      expectEnabled: false,
    },
  ])("handles enabling browser notifications when permission is $name", async ({ granted, expectEnabled }) => {
    mockRequestPermission.mockResolvedValue(granted);
    await renderReady();

    fireEvent.click(screen.getByRole("button", { name: "启用通知" }));

    await waitFor(() => {
      expect(mockRequestPermission).toHaveBeenCalled();
    });
    if (expectEnabled) {
      expect(mockSaveNotificationSettings).toHaveBeenCalledWith(
        expect.objectContaining({ enabled: true }),
      );
      expect(screen.getByRole("button", { name: "下载完成时通知" })).toBeInTheDocument();
    } else {
      expect(showToastMock).toHaveBeenCalledWith(
        "浏览器通知权限被拒绝，请在浏览器设置中允许通知",
        "warning",
      );
      expect(mockSaveNotificationSettings).not.toHaveBeenCalled();
    }
  });

  test("toggles notification options and disables notifications", async () => {
    await renderReady();

    fireEvent.click(screen.getByRole("button", { name: "启用通知" }));
    await screen.findByRole("button", { name: "下载完成时通知" });

    fireEvent.click(screen.getByRole("button", { name: "下载完成时通知" }));
    expect(mockSaveNotificationSettings).toHaveBeenLastCalledWith(
      expect.objectContaining({ onComplete: false }),
    );
    fireEvent.click(screen.getByRole("button", { name: "下载失败时通知" }));
    expect(mockSaveNotificationSettings).toHaveBeenLastCalledWith(
      expect.objectContaining({ onError: false }),
    );

    fireEvent.click(screen.getByRole("button", { name: "启用通知" }));
    expect(mockSaveNotificationSettings).toHaveBeenLastCalledWith(
      expect.objectContaining({ enabled: false }),
    );
  });

  test("starts with notification options from saved settings", async () => {
    (getNotificationSettings as jest.Mock).mockReturnValue({
      enabled: true,
      onComplete: false,
      onError: true,
    });
    await renderReady();

    expect(screen.getByRole("button", { name: "下载完成时通知" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "下载失败时通知" }));
    expect(mockSaveNotificationSettings).toHaveBeenCalledWith({
      enabled: true,
      onComplete: false,
      onError: false,
    });
  });

  test("enables rpc access and shows the secret panel", async () => {
    mockApi.setRpcAccess.mockResolvedValue(enabledRpcAccess as never);

    await renderReady();
    fireEvent.click(screen.getByRole("button", { name: "允许外部 aria2 客户端连接" }));

    await waitFor(() => {
      expect(mockApi.setRpcAccess).toHaveBeenCalledWith(true);
    });
    expect(await screen.findByText("RPC 密钥")).toBeInTheDocument();
    expect(screen.getByText("rpc-secret")).toBeInTheDocument();
  });

  test("shows error when toggling rpc access fails", async () => {
    const errorSpy = jest.spyOn(console, "error").mockImplementation(() => {});
    mockApi.setRpcAccess.mockRejectedValue(new Error("rpc toggle failed") as never);

    try {
      await renderReady();
      fireEvent.click(screen.getByRole("button", { name: "允许外部 aria2 客户端连接" }));

      expect(await screen.findByText("设置 RPC 访问失败: rpc toggle failed")).toBeInTheDocument();
      expect(errorSpy).toHaveBeenCalledWith(
        "设置 RPC 访问失败",
        expect.any(Error),
      );
    } finally {
      errorSpy.mockRestore();
    }
  });

  test("does not refresh the secret when confirmation is cancelled", async () => {
    mockApi.getRpcAccess.mockResolvedValue(enabledRpcAccess as never);
    showConfirmMock.mockResolvedValue(false);

    await renderReady();
    fireEvent.click(screen.getByRole("button", { name: "刷新" }));

    await waitFor(() => {
      expect(showConfirmMock).toHaveBeenCalledWith(
        expect.objectContaining({ title: "刷新密钥", danger: true }),
      );
    });
    expect(mockApi.refreshRpcSecret).not.toHaveBeenCalled();
  });

  test("refreshes the secret after confirmation", async () => {
    mockApi.getRpcAccess.mockResolvedValue(enabledRpcAccess as never);
    showConfirmMock.mockResolvedValue(true);
    mockApi.refreshRpcSecret.mockResolvedValue({
      ...enabledRpcAccess,
      secret: "new-secret",
    } as never);

    await renderReady();
    fireEvent.click(screen.getByRole("button", { name: "刷新" }));

    await waitFor(() => {
      expect(mockApi.refreshRpcSecret).toHaveBeenCalled();
    });
    expect(await screen.findByText("new-secret")).toBeInTheDocument();
  });

  test("shows error when refreshing the secret fails", async () => {
    const errorSpy = jest.spyOn(console, "error").mockImplementation(() => {});
    mockApi.getRpcAccess.mockResolvedValue(enabledRpcAccess as never);
    showConfirmMock.mockResolvedValue(true);
    mockApi.refreshRpcSecret.mockRejectedValue(new Error("refresh failed") as never);

    try {
      await renderReady();
      fireEvent.click(screen.getByRole("button", { name: "刷新" }));

      expect(await screen.findByText("刷新 Secret 失败: refresh failed")).toBeInTheDocument();
      expect(errorSpy).toHaveBeenCalledWith(
        "刷新 Secret 失败",
        expect.any(Error),
      );
    } finally {
      errorSpy.mockRestore();
    }
  });

  test("copies secret and rpc url to clipboard", async () => {
    jest.useFakeTimers();
    mockApi.getRpcAccess.mockResolvedValue(enabledRpcAccess as never);

    try {
      await renderReady();
      const copyButtons = screen.getAllByRole("button", { name: "复制" });
      fireEvent.click(copyButtons[0]);
      fireEvent.click(copyButtons[1]);

      await waitFor(() => {
        expect(navigator.clipboard.writeText).toHaveBeenCalledWith("rpc-secret");
      });
      expect(navigator.clipboard.writeText).toHaveBeenCalledWith(
        `${window.location.origin}/aria2/jsonrpc`,
      );
      expect(showToastMock).toHaveBeenCalledWith("已复制", "success");
      expect(await screen.findAllByText("已复制")).toHaveLength(2);

      act(() => {
        jest.advanceTimersByTime(2000);
      });
      await waitFor(() => {
        expect(screen.getAllByRole("button", { name: "复制" })).toHaveLength(2);
      });
    } finally {
      jest.useRealTimers();
    }
  });

  test("ignores clipboard success callbacks after unmount", async () => {
    mockApi.getRpcAccess.mockResolvedValue(enabledRpcAccess as never);
    let resolveCopy: (() => void) = () => {};
    (navigator.clipboard.writeText as jest.Mock).mockReturnValue(
      new Promise<void>((done) => {
        resolveCopy = done;
      }),
    );

    const utils = render(<ProfilePage />);
    await screen.findByRole("heading", { name: "用户设置" });
    fireEvent.click(screen.getAllByRole("button", { name: "复制" })[0]);
    fireEvent.click(screen.getAllByRole("button", { name: "复制" })[1]);
    utils.unmount();

    await act(async () => {
      resolveCopy();
      await Promise.resolve();
    });
    expect(screen.queryByText("已复制")).not.toBeInTheDocument();
  });

  test("shows error toast when copy secret/url fails", async () => {
    mockApi.getRpcAccess.mockResolvedValue(enabledRpcAccess as never);
    (navigator.clipboard.writeText as jest.Mock).mockRejectedValue(new Error("copy failed"));

    render(<ProfilePage />);
    expect(await screen.findByRole("heading", { name: "用户设置" })).toBeInTheDocument();

    const copyButtons = screen.getAllByRole("button", { name: "复制" });
    fireEvent.click(copyButtons[0]);
    fireEvent.click(copyButtons[1]);

    await waitFor(() => {
      expect(showToastMock).toHaveBeenCalledWith("复制失败", "error");
    });
  });

  test.each([
    {
      name: "json without detail field",
      error: new Error(JSON.stringify({})),
    },
    {
      name: "empty error message",
      error: new Error(""),
    },
  ])("falls back to generic message when change password fails ($name)", async ({ error }) => {
    mockApi.changePassword.mockRejectedValue(error as never);
    const { container } = render(<ProfilePage />);

    expect(await screen.findByText("用户设置")).toBeInTheDocument();
    const passwordInputs = container.querySelectorAll("input[type='password']");
    fireEvent.change(passwordInputs[0], { target: { value: "old-pass" } });
    fireEvent.change(passwordInputs[1], { target: { value: "new-pass-1" } });
    fireEvent.change(passwordInputs[2], { target: { value: "new-pass-1" } });
    fireEvent.click(screen.getByRole("button", { name: "修改密码" }));

    expect(await screen.findAllByText("密码修改失败")).toHaveLength(1);
  });

  test("ignores rpc toggle failure after unmount", async () => {
    let rejectToggle: (reason?: unknown) => void = () => {};
    mockApi.setRpcAccess.mockReturnValue(
      new Promise((_res, rej) => {
        rejectToggle = rej;
      }) as never,
    );

    const utils = render(<ProfilePage />);
    await screen.findByRole("heading", { name: "用户设置" });
    fireEvent.click(screen.getByRole("button", { name: "允许外部 aria2 客户端连接" }));
    utils.unmount();

    const errorSpy = jest.spyOn(console, "error").mockImplementation(() => {});
    await act(async () => {
      rejectToggle(new Error("late failure"));
      await Promise.resolve();
    });
    errorSpy.mockRestore();
  });

  test("ignores refresh secret result after unmount", async () => {
    mockApi.getRpcAccess.mockResolvedValue(enabledRpcAccess as never);
    showConfirmMock.mockResolvedValue(true);
    let rejectRefresh: (reason?: unknown) => void = () => {};
    mockApi.refreshRpcSecret.mockReturnValue(
      new Promise((_res, rej) => {
        rejectRefresh = rej;
      }) as never,
    );

    const utils = render(<ProfilePage />);
    await screen.findByRole("heading", { name: "用户设置" });
    fireEvent.click(screen.getByRole("button", { name: "刷新" }));
    await waitFor(() => {
      expect(mockApi.refreshRpcSecret).toHaveBeenCalled();
    });
    utils.unmount();

    const errorSpy = jest.spyOn(console, "error").mockImplementation(() => {});
    await act(async () => {
      rejectRefresh(new Error("late failure"));
      await Promise.resolve();
    });
    errorSpy.mockRestore();
  });

  test("ignores change password failure after unmount", async () => {
    let rejectChange: (reason?: unknown) => void = () => {};
    mockApi.changePassword.mockReturnValue(
      new Promise((_res, rej) => {
        rejectChange = rej;
      }) as never,
    );

    const { container, unmount } = render(<ProfilePage />);
    await screen.findByText("用户设置");
    const passwordInputs = container.querySelectorAll("input[type='password']");
    fireEvent.change(passwordInputs[0], { target: { value: "old-pass" } });
    fireEvent.change(passwordInputs[1], { target: { value: "new-pass-1" } });
    fireEvent.change(passwordInputs[2], { target: { value: "new-pass-1" } });
    fireEvent.click(screen.getByRole("button", { name: "修改密码" }));
    unmount();

    await act(async () => {
      rejectChange(new Error("late failure"));
      await Promise.resolve();
    });
  });

  test("ignores refresh confirmation after unmount", async () => {
    mockApi.getRpcAccess.mockResolvedValue(enabledRpcAccess as never);
    let resolveConfirm: (value: boolean) => void = () => {};
    showConfirmMock.mockReturnValue(
      new Promise<boolean>((done) => {
        resolveConfirm = done;
      }) as never,
    );

    const utils = render(<ProfilePage />);
    await screen.findByRole("heading", { name: "用户设置" });
    fireEvent.click(screen.getByRole("button", { name: "刷新" }));
    utils.unmount();

    await act(async () => {
      resolveConfirm(true);
      await Promise.resolve();
    });
    expect(mockApi.refreshRpcSecret).not.toHaveBeenCalled();
  });

  test("shows initial password alert from url param and closes it", async () => {
    window.history.pushState({}, "", "/profile?initial_password=1");

    await renderReady();

    expect(screen.getByLabelText("初始密码安全提醒")).toBeInTheDocument();
    expect(
      screen.getByText(/您当前使用的是管理员设置的初始密码/),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "我知道了" }));
    expect(screen.queryByLabelText("初始密码安全提醒")).not.toBeInTheDocument();
  });
});
