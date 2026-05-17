import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import LoginPage from "@/app/login/page";
import { api, ApiError } from "@/lib/api";
import type { User } from "@/types";

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
      me: jest.fn<Promise<User>, []>(),
      getSiteInfo: jest.fn<Promise<{ site_title: string }>, []>(),
      login: jest.fn<Promise<User>, [string, string]>(),
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

function createDeferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("LoginPage", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockApi.getSiteInfo.mockResolvedValue({ site_title: "Test Site" });
  });

  test("redirects to tasks when existing session is valid", async () => {
    mockApi.me.mockResolvedValue(normalUser);

    render(<LoginPage />);

    await waitFor(() => {
      expect(pushMock).toHaveBeenCalledWith("/tasks");
    });
    expect(await screen.findByText("Test Site")).toBeInTheDocument();
  });

  test("redirects to profile when current user uses initial password", async () => {
    mockApi.me.mockResolvedValue({ ...normalUser, is_initial_password: true });

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

  test("keeps password input focused while editing password", async () => {
    mockApi.me.mockRejectedValue(new ApiError("unauthorized", 401, true));

    render(<LoginPage />);

    await screen.findByPlaceholderText("用户名");
    const passwordInput = screen.getByPlaceholderText("密码");

    passwordInput.focus();
    fireEvent.change(passwordInput, {
      target: { value: "a" },
    });

    expect(document.activeElement).toBe(passwordInput);
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
    mockApi.login.mockRejectedValue(new ApiError("unauthorized", 401, true));

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

  test("shows network error when login request cannot reach server", async () => {
    mockApi.me.mockRejectedValue(new ApiError("unauthorized", 401, true));
    mockApi.login.mockRejectedValue(new ApiError("network", 0, false, true));

    render(<LoginPage />);

    fireEvent.change(await screen.findByPlaceholderText("用户名"), {
      target: { value: "alice" },
    });
    fireEvent.change(screen.getByPlaceholderText("密码"), {
      target: { value: "bad-password" },
    });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));

    expect(await screen.findByText("网络连接失败，请检查地址或网络")).toBeInTheDocument();
  });

  test("shows backend error message for non-401 API errors", async () => {
    mockApi.me.mockRejectedValue(new ApiError("unauthorized", 401, true));
    mockApi.login.mockRejectedValue(new ApiError("服务暂时不可用", 503, false, false));

    render(<LoginPage />);

    fireEvent.change(await screen.findByPlaceholderText("用户名"), {
      target: { value: "alice" },
    });
    fireEvent.change(screen.getByPlaceholderText("密码"), {
      target: { value: "bad-password" },
    });
    fireEvent.click(screen.getByRole("button", { name: "登录" }));

    expect(await screen.findByText("服务暂时不可用")).toBeInTheDocument();
  });

  test("redirects to tasks after successful login", async () => {
    const errorSpy = jest.spyOn(console, "error").mockImplementation(() => undefined);
    mockApi.me.mockRejectedValue(new ApiError("unauthorized", 401, true));
    mockApi.login.mockResolvedValue(normalUser);

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
    expect(pushMock).not.toHaveBeenCalled();
    errorSpy.mockRestore();
  });

  test("redirects to profile after successful login with initial password", async () => {
    const errorSpy = jest.spyOn(console, "error").mockImplementation(() => undefined);
    mockApi.me.mockRejectedValue(new ApiError("unauthorized", 401, true));
    mockApi.login.mockResolvedValue({ ...normalUser, is_initial_password: true });

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
    expect(pushMock).not.toHaveBeenCalled();
    errorSpy.mockRestore();
  });

  test("does not redirect after unmount when async checks resolve late", async () => {
    const meDeferred = createDeferred<User>();
    const siteInfoDeferred = createDeferred<{ site_title: string }>();
    mockApi.me.mockReturnValue(meDeferred.promise);
    mockApi.getSiteInfo.mockReturnValue(siteInfoDeferred.promise);

    const { unmount } = render(<LoginPage />);
    unmount();

    await act(async () => {
      meDeferred.resolve(normalUser);
      siteInfoDeferred.resolve({ site_title: "Late Site" });
      await Promise.allSettled([meDeferred.promise, siteInfoDeferred.promise]);
      await Promise.resolve();
    });

    expect(pushMock).not.toHaveBeenCalled();
  });
});
