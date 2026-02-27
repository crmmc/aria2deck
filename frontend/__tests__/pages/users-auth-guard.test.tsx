import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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

  test("blocks create user when quota is invalid", async () => {
    mockApi.me.mockResolvedValue(adminUser as never);
    mockApi.listUsers.mockResolvedValue([adminUser] as never);

    const { container } = renderPage();

    expect(await screen.findByText("创建新用户")).toBeInTheDocument();
    const textInput = container.querySelector("input:not([type]), input[type='text']");
    const passwordInput = container.querySelector("input[type='password']");
    expect(textInput).not.toBeNull();
    expect(passwordInput).not.toBeNull();
    fireEvent.change(textInput as HTMLInputElement, { target: { value: "new-user" } });
    fireEvent.change(passwordInput as HTMLInputElement, { target: { value: "pass123456" } });
    const quotaInput = container.querySelector("input[type='number']");
    expect(quotaInput).not.toBeNull();
    fireEvent.change(quotaInput as HTMLInputElement, { target: { value: "0" } });
    const form = container.querySelector("form.create-user-form");
    expect(form).not.toBeNull();
    fireEvent.submit(form as HTMLFormElement);

    expect(await screen.findByText("配额必须为正数")).toBeInTheDocument();
    expect(mockApi.createUser).not.toHaveBeenCalled();
  });
});
