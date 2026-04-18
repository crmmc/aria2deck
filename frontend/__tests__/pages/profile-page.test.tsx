import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import ProfilePage from "@/app/(authenticated)/profile/page";
import { api } from "@/lib/api";

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
  getNotificationSettings: () => ({ enabled: false, onComplete: true, onError: true }),
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

describe("ProfilePage", () => {
  beforeEach(() => {
    jest.clearAllMocks();
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
    mockApi.getRpcAccess.mockResolvedValue({
      enabled: false,
      secret: null,
      created_at: null,
    } as never);
    mockApi.changePassword.mockResolvedValue({ ok: true, message: "ok" } as never);
  });

  test("renders profile settings", async () => {
    render(<ProfilePage />);

    expect(await screen.findByRole("heading", { name: "用户设置" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "修改密码", level: 2 })).toBeInTheDocument();
    expect(mockApi.getRpcAccess).toHaveBeenCalled();
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

  test("shows error toast when copy secret/url fails", async () => {
    mockApi.getRpcAccess.mockResolvedValue({
      enabled: true,
      secret: "rpc-secret",
      created_at: "2024-01-01T00:00:00Z",
    } as never);
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
});
