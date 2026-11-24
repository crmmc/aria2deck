import { act, fireEvent, render, screen } from "@testing-library/react";
import LoginPage from "@/app/login/page";
import { api, ApiError } from "@/lib/api";
import type { User } from "@/types";

jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: jest.fn() }),
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

async function submitInvalidLogin() {
  fireEvent.change(await screen.findByPlaceholderText("用户名"), {
    target: { value: "alice" },
  });
  fireEvent.change(screen.getByPlaceholderText("密码"), {
    target: { value: "secret" },
  });
  await act(async () => {
    fireEvent.click(screen.getByRole("button", { name: "登录" }));
  });
}

describe("LoginPage error fallbacks", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockApi.me.mockRejectedValue(new ApiError("unauthorized", 401, true));
    mockApi.getSiteInfo.mockResolvedValue({ site_title: "Test Site" });
  });

  it.each([
    {
      name: "plain Error",
      error: new Error("自定义错误"),
      expected: "自定义错误",
    },
    {
      name: "non-Error rejection",
      error: "just a string",
      expected: "登录失败，请稍后重试",
    },
    {
      name: "empty ApiError message",
      error: new ApiError("", 502),
      expected: "登录失败，请稍后重试",
    },
  ])("shows '$expected' for $name", async ({ error, expected }) => {
    mockApi.login.mockRejectedValue(error as never);

    render(<LoginPage />);

    await submitInvalidLogin();

    expect(await screen.findByText(expected)).toBeInTheDocument();
  });
});
