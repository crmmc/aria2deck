import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import LoginPage from "@/app/login/page";
import { api, ApiError } from "@/lib/api";

const pushMock = jest.fn();

jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock }),
}));

jest.mock("@/lib/api", () => {
  class MockApiError extends Error {
    status: number;
    isUnauthorized: boolean;
    isNetworkError: boolean;

    constructor(message: string, status: number, isUnauthorized = false, isNetworkError = false) {
      super(message);
      this.status = status;
      this.isUnauthorized = isUnauthorized;
      this.isNetworkError = isNetworkError;
    }
  }

  return {
    __esModule: true,
    api: {
      me: jest.fn(),
      getSiteInfo: jest.fn(),
      login: jest.fn(),
    },
    ApiError: MockApiError,
  };
});

const mockApi = api as jest.Mocked<typeof api>;

const normalUser = {
  id: 1,
  username: "admin",
  is_admin: true,
  quota: 1024 * 1024 * 1024,
  is_initial_password: false,
};

describe("LoginPage", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockApi.getSiteInfo.mockResolvedValue({ site_title: "Test Site" } as never);
  });

  test("redirects to tasks when existing session is valid", async () => {
    mockApi.me.mockResolvedValue(normalUser as never);

    render(<LoginPage />);

    await waitFor(() => {
      expect(pushMock).toHaveBeenCalledWith("/tasks");
    });
    expect(await screen.findByText("Test Site")).toBeInTheDocument();
  });

  test("redirects to profile when current user uses initial password", async () => {
    mockApi.me.mockResolvedValue({ ...normalUser, is_initial_password: true } as never);

    render(<LoginPage />);

    await waitFor(() => {
      expect(pushMock).toHaveBeenCalledWith("/profile?initial_password=1");
    });
  });

  test("shows login form when unauthorized", async () => {
    mockApi.me.mockRejectedValue(new ApiError("unauthorized", 401, true));

    render(<LoginPage />);

    expect(await screen.findByPlaceholderText("用户名")).toBeInTheDocument();
    expect(pushMock).not.toHaveBeenCalled();
  });

  test("logs warnings when auto-login or site info request fails unexpectedly", async () => {
    const warnSpy = jest.spyOn(console, "warn").mockImplementation(() => undefined);
    mockApi.me.mockRejectedValue(new Error("session check failed"));
    mockApi.getSiteInfo.mockRejectedValue(new Error("site info failed"));

    render(<LoginPage />);

    expect(await screen.findByPlaceholderText("用户名")).toBeInTheDocument();
    await waitFor(() => {
      expect(warnSpy).toHaveBeenCalledWith("自动登录检查失败", expect.any(Error));
      expect(warnSpy).toHaveBeenCalledWith("加载站点标题失败", expect.any(Error));
    });
    warnSpy.mockRestore();
  });

  test("submits login and shows error when credentials are invalid", async () => {
    mockApi.me.mockRejectedValue(new ApiError("unauthorized", 401, true));
    mockApi.login.mockRejectedValue(new Error("invalid"));

    render(<LoginPage />);

    fireEvent.change(await screen.findByPlaceholderText("用户名"), {
      target: { value: "alice" },
    });
    fireEvent.change(screen.getByPlaceholderText("密码"), {
      target: { value: "bad-password" },
    });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));

    await waitFor(() => {
      expect(mockApi.login).toHaveBeenCalledWith("alice", "bad-password");
    });
    expect(await screen.findByText("用户名或密码无效")).toBeInTheDocument();
  });

  test("redirects to tasks after successful login", async () => {
    const errorSpy = jest.spyOn(console, "error").mockImplementation(() => undefined);
    mockApi.me.mockRejectedValue(new ApiError("unauthorized", 401, true));
    mockApi.login.mockResolvedValue(normalUser as never);

    render(<LoginPage />);

    fireEvent.change(await screen.findByPlaceholderText("用户名"), {
      target: { value: "admin" },
    });
    fireEvent.change(screen.getByPlaceholderText("密码"), {
      target: { value: "123456" },
    });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));

    await waitFor(() => {
      expect(mockApi.login).toHaveBeenCalledWith("admin", "123456");
    });
    errorSpy.mockRestore();
  });

  test("redirects to profile after successful login with initial password", async () => {
    const errorSpy = jest.spyOn(console, "error").mockImplementation(() => undefined);
    mockApi.me.mockRejectedValue(new ApiError("unauthorized", 401, true));
    mockApi.login.mockResolvedValue({ ...normalUser, is_initial_password: true } as never);

    render(<LoginPage />);

    fireEvent.change(await screen.findByPlaceholderText("用户名"), {
      target: { value: "admin" },
    });
    fireEvent.change(screen.getByPlaceholderText("密码"), {
      target: { value: "123456" },
    });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));

    await waitFor(() => {
      expect(mockApi.login).toHaveBeenCalledWith("admin", "123456");
    });
    errorSpy.mockRestore();
  });
});
