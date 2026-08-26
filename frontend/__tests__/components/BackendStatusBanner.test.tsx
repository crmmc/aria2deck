import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import BackendStatusBanner from "@/components/BackendStatusBanner";

const pushMock = jest.fn();
const getSystemStatusMock = jest.fn();

jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock }),
}));

jest.mock("@/lib/api", () => ({
  api: {
    getSystemStatus: (...args: unknown[]) => getSystemStatusMock(...args),
  },
}));

const user = {
  id: 1,
  username: "user",
  is_admin: false,
  quota: 1024,
};

const admin = {
  ...user,
  username: "admin",
  is_admin: true,
};

const hiddenDescriptor = Object.getOwnPropertyDescriptor(Document.prototype, "hidden");
const visibilityStateDescriptor = Object.getOwnPropertyDescriptor(
  Document.prototype,
  "visibilityState",
);

function stubDocumentVisibility(hidden: boolean) {
  Object.defineProperty(document, "hidden", {
    configurable: true,
    get: () => hidden,
  });
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => (hidden ? "hidden" : "visible"),
  });
}

describe("BackendStatusBanner", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    jest.useFakeTimers();
  });

  afterEach(() => {
    if (hiddenDescriptor) {
      Object.defineProperty(document, "hidden", hiddenDescriptor);
    }
    if (visibilityStateDescriptor) {
      Object.defineProperty(document, "visibilityState", visibilityStateDescriptor);
    }
    jest.useRealTimers();
  });

  test("does not render when backend is ok", async () => {
    getSystemStatusMock.mockResolvedValue({
      download_backend: { status: "ok", message: "服务运行正常" },
    });

    render(<BackendStatusBanner user={user} />);

    await waitFor(() => {
      expect(getSystemStatusMock).toHaveBeenCalled();
    });
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  test("shows generic message for regular users", async () => {
    getSystemStatusMock.mockResolvedValue({
      download_backend: {
        status: "degraded",
        message: "服务器遇到错误，请联系管理员",
      },
    });

    render(<BackendStatusBanner user={user} />);

    expect(await screen.findByRole("status")).toBeInTheDocument();
    expect(screen.getByText("服务异常")).toBeInTheDocument();
    expect(screen.getByText("服务器遇到错误，请联系管理员")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "查看系统设置" })).not.toBeInTheDocument();
  });

  test("shows admin copy and settings action", async () => {
    getSystemStatusMock.mockResolvedValue({
      download_backend: {
        status: "degraded",
        message: "无法连接到下载后端",
      },
    });

    render(<BackendStatusBanner user={admin} />);

    expect(await screen.findByText("下载后端异常")).toBeInTheDocument();
    expect(screen.getByText("无法连接到下载后端")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "查看系统设置" }));
    expect(pushMock).toHaveBeenCalledWith("/settings");
  });

  test("can be dismissed until status recovers", async () => {
    getSystemStatusMock
      .mockResolvedValueOnce({
        download_backend: {
          status: "degraded",
          message: "服务器遇到错误，请联系管理员",
        },
      })
      .mockResolvedValueOnce({
        download_backend: {
          status: "degraded",
          message: "服务器遇到错误，请联系管理员",
        },
      })
      .mockResolvedValueOnce({
        download_backend: {
          status: "ok",
          message: "服务运行正常",
        },
      })
      .mockResolvedValueOnce({
        download_backend: {
          status: "degraded",
          message: "服务器遇到错误，请联系管理员",
        },
      });

    render(<BackendStatusBanner user={user} />);

    expect(await screen.findByRole("status")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "关闭服务状态提醒" }));
    expect(screen.queryByRole("status")).not.toBeInTheDocument();

    await act(async () => {
      jest.advanceTimersByTime(20_000);
    });
    // Still degraded and dismissed.
    expect(screen.queryByRole("status")).not.toBeInTheDocument();

    await act(async () => {
      jest.advanceTimersByTime(20_000);
    });
    // Recovered: banner stays hidden.
    expect(screen.queryByRole("status")).not.toBeInTheDocument();

    await act(async () => {
      jest.advanceTimersByTime(20_000);
    });
    // Degraded again after recovery: banner returns.
    expect(await screen.findByRole("status")).toBeInTheDocument();
  });

  test("skips interval polling while hidden and refreshes on becoming visible", async () => {
    getSystemStatusMock.mockResolvedValue({
      download_backend: { status: "ok", message: "服务运行正常" },
    });

    render(<BackendStatusBanner user={user} />);

    // Initial mount poll completes.
    await waitFor(() => {
      expect(getSystemStatusMock).toHaveBeenCalledTimes(1);
    });

    await act(async () => {
      stubDocumentVisibility(true);
      jest.advanceTimersByTime(20_000);
    });
    // Hidden tab: the 20s interval must not poll again.
    expect(getSystemStatusMock).toHaveBeenCalledTimes(1);

    await act(async () => {
      stubDocumentVisibility(false);
      document.dispatchEvent(new Event("visibilitychange"));
    });
    // Becoming visible triggers an immediate refresh.
    expect(getSystemStatusMock).toHaveBeenCalledTimes(2);
  });
});
