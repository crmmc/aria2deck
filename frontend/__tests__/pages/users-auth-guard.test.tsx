import { render, screen, waitFor } from "@testing-library/react";
import UsersPage from "@/app/(authenticated)/users/page";
import { ToastProvider } from "@/components/Toast";
import { api } from "@/lib/api";

const pushMock = jest.fn();

jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock }),
}));

jest.mock("@/lib/api", () => ({
  __esModule: true,
  api: {
    me: jest.fn(),
    listUsers: jest.fn(),
    createUser: jest.fn(),
    updateUser: jest.fn(),
    deleteUser: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

const adminUser = {
  id: 1,
  username: "admin",
  is_admin: true,
  quota: 1024 * 1024 * 1024,
  is_initial_password: false,
};

const normalUser = {
  ...adminUser,
  id: 2,
  username: "user",
  is_admin: false,
};

function renderPage() {
  return render(
    <ToastProvider>
      <UsersPage />
    </ToastProvider>
  );
}

describe("UsersPage auth guard", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    jest.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    (console.error as jest.Mock).mockRestore?.();
  });

  test("redirects non-admin users and does not keep loading forever", async () => {
    mockApi.me.mockResolvedValue(normalUser as never);

    renderPage();

    await waitFor(() => {
      expect(pushMock).toHaveBeenCalledWith("/tasks");
    });
    await waitFor(() => {
      expect(screen.queryByText("用户")).not.toBeInTheDocument();
    });
    expect(mockApi.listUsers).not.toHaveBeenCalled();
  });

  test("stops loading when initial auth check fails", async () => {
    mockApi.me.mockRejectedValue(new Error("network down"));

    renderPage();

    await waitFor(() => {
      expect(screen.queryByText("用户")).not.toBeInTheDocument();
    });
    expect(console.error).toHaveBeenCalled();
  });

  test("loads user list for admin", async () => {
    mockApi.me.mockResolvedValue(adminUser as never);
    mockApi.listUsers.mockResolvedValue([
      adminUser,
      { ...normalUser, username: "alice" },
    ] as never);

    renderPage();

    await waitFor(() => {
      expect(screen.getByText("alice")).toBeInTheDocument();
    });
  });
});
