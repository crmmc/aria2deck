import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import ProfilePage from "@/app/(authenticated)/profile/page";
import { api } from "@/lib/api";

const showToastMock = jest.fn();
const showConfirmMock = jest.fn();
const refreshUserMock = jest.fn();

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
    user: {
      id: 1,
      username: "admin",
      is_admin: true,
      quota: 1024 * 1024 * 1024,
      is_initial_password: false,
    },
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

  test("shows validation error when new passwords do not match", async () => {
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
      target: { value: "new-pass-2" },
    });
    fireEvent.click(screen.getByRole("button", { name: "修改密码" }));

    await waitFor(() => {
      expect(screen.getByText("两次输入的新密码不一致")).toBeInTheDocument();
    });
  });

  test("shows validation error when new password is too short", async () => {
    const { container } = render(<ProfilePage />);

    expect(await screen.findByText("用户设置")).toBeInTheDocument();
    const passwordInputs = container.querySelectorAll("input[type='password']");
    fireEvent.change(passwordInputs[0], {
      target: { value: "old-pass" },
    });
    fireEvent.change(passwordInputs[1], {
      target: { value: "123" },
    });
    fireEvent.change(passwordInputs[2], {
      target: { value: "123" },
    });
    fireEvent.click(screen.getByRole("button", { name: "修改密码" }));

    await waitFor(() => {
      expect(screen.getByText("新密码长度至少为 6 位")).toBeInTheDocument();
    });
  });
});
