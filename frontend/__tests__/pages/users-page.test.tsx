import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import UsersPage from "@/app/(authenticated)/users/page";
import { api } from "@/lib/api";

const pushMock = jest.fn();
const showToastMock = jest.fn();

jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock }),
}));

jest.mock("@/components/Toast", () => ({
  __esModule: true,
  useToast: () => ({
    showToast: showToastMock,
  }),
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

const managedUser = {
  ...normalUser,
  id: 3,
  username: "alice",
  quota: 2 * 1024 * 1024 * 1024,
};

function renderPage() {
  return render(<UsersPage />);
}

async function renderAdminPage() {
  mockApi.me.mockResolvedValue(adminUser as never);
  mockApi.listUsers.mockResolvedValue([adminUser, managedUser] as never);
  const utils = renderPage();
  expect(await screen.findByText("创建新用户")).toBeInTheDocument();
  expect(await screen.findByText("alice")).toBeInTheDocument();
  return utils;
}

function getCreateForm(container: HTMLElement) {
  const form = container.querySelector("form.create-user-form") as HTMLFormElement;
  const usernameInput = form.querySelector("input:not([type]), input[type='text']") as HTMLInputElement;
  const passwordInput = form.querySelector("input[type='password']") as HTMLInputElement;
  const quotaInput = form.querySelector("input[type='number']") as HTMLInputElement;
  const quotaUnitSelect = form.querySelector("select") as HTMLSelectElement;
  const adminCheckbox = form.querySelector("input[type='checkbox']") as HTMLInputElement;
  return { form, usernameInput, passwordInput, quotaInput, quotaUnitSelect, adminCheckbox };
}

describe("UsersPage", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    jest.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    (console.error as jest.Mock).mockRestore?.();
  });

  test("redirects non-admin users without loading the user list", async () => {
    mockApi.me.mockResolvedValue(normalUser as never);

    renderPage();

    await waitFor(() => {
      expect(pushMock).toHaveBeenCalledWith("/tasks");
    });
    expect(mockApi.listUsers).not.toHaveBeenCalled();
    expect(screen.queryByText("用户")).not.toBeInTheDocument();
  });

  test("shows load error when the initial auth check fails", async () => {
    mockApi.me.mockRejectedValue(new Error("network down"));

    renderPage();

    expect(await screen.findByText("加载用户列表失败")).toBeInTheDocument();
    expect(console.error).toHaveBeenCalled();
  });

  test("blocks create user when quota is invalid", async () => {
    const { container } = await renderAdminPage();
    const { form, usernameInput, passwordInput, quotaInput } = getCreateForm(container);

    fireEvent.change(usernameInput, { target: { value: "new-user" } });
    fireEvent.change(passwordInput, { target: { value: "pass123456" } });
    fireEvent.change(quotaInput, { target: { value: "0" } });
    fireEvent.submit(form);

    expect(await screen.findByText("配额必须为正数")).toBeInTheDocument();
    expect(mockApi.createUser).not.toHaveBeenCalled();
  });

  test("creates a user and resets the form", async () => {
    const createdUser = {
      ...normalUser,
      id: 4,
      username: "new-user",
      is_admin: true,
      quota: 2 * 1024 * 1024,
    };
    mockApi.createUser.mockResolvedValue(createdUser as never);

    const { container } = await renderAdminPage();
    const { form, usernameInput, passwordInput, quotaInput, quotaUnitSelect, adminCheckbox } =
      getCreateForm(container);

    fireEvent.change(usernameInput, { target: { value: "new-user" } });
    fireEvent.change(passwordInput, { target: { value: "pass123456" } });
    fireEvent.click(adminCheckbox);
    fireEvent.change(quotaInput, { target: { value: "2" } });
    fireEvent.change(quotaUnitSelect, { target: { value: "MB" } });
    fireEvent.submit(form);

    await waitFor(() => {
      expect(mockApi.createUser).toHaveBeenCalledWith({
        username: "new-user",
        password: "pass123456",
        is_admin: true,
        quota: 2 * 1024 * 1024,
      });
    });
    expect(usernameInput).toHaveValue("");
    expect(passwordInput).toHaveValue("");
    expect(quotaInput).toHaveValue(100);
    expect(quotaUnitSelect).toHaveValue("GB");
    expect(adminCheckbox).not.toBeChecked();
  });

  test("updates an existing user with changed fields only", async () => {
    const updatedUser = {
      ...managedUser,
      username: "alice-updated",
      is_admin: true,
      quota: 3 * 1024 * 1024 * 1024,
    };
    mockApi.updateUser.mockResolvedValue(updatedUser as never);

    await renderAdminPage();

    const managedRow = screen.getByText("alice").closest("tr") as HTMLElement;
    fireEvent.click(within(managedRow).getByRole("button", { name: "编辑" }));

    const modal = await screen.findByText("编辑用户");
    const modalContent = modal.closest(".modal-content") as HTMLElement;
    const usernameInput = within(modalContent).getByDisplayValue("alice");
    const passwordInput = modalContent.querySelector("input[type='password']") as HTMLInputElement;
    const quotaInput = modalContent.querySelector("input[type='number']") as HTMLInputElement;
    const quotaUnitSelect = modalContent.querySelector("select") as HTMLSelectElement;
    const adminCheckbox = within(modalContent).getByRole("checkbox");

    fireEvent.change(usernameInput, { target: { value: "alice-updated" } });
    fireEvent.change(passwordInput, { target: { value: "changed-pass" } });
    fireEvent.click(adminCheckbox);
    fireEvent.change(quotaInput, { target: { value: "3" } });
    fireEvent.change(quotaUnitSelect, { target: { value: "GB" } });
    fireEvent.click(within(modalContent).getByRole("button", { name: "保存更改" }));

    await waitFor(() => {
      expect(mockApi.updateUser).toHaveBeenCalledWith(
        3,
        {
          username: "alice-updated",
          password: "changed-pass",
          is_admin: true,
          quota: 3 * 1024 * 1024 * 1024,
        },
        "alice"
      );
    });
    expect(screen.queryByText("编辑用户")).not.toBeInTheDocument();
  });

  test("deletes a user and shows success toast", async () => {
    mockApi.deleteUser.mockResolvedValue({ ok: true } as never);

    await renderAdminPage();

    const managedRow = screen.getByText("alice").closest("tr") as HTMLElement;
    fireEvent.click(within(managedRow).getByRole("button", { name: "删除" }));

    const modal = await screen.findByText("删除用户");
    const modalContent = modal.closest(".modal-content") as HTMLElement;
    fireEvent.click(within(modalContent).getByRole("button", { name: "删除" }));

    await waitFor(() => {
      expect(mockApi.deleteUser).toHaveBeenCalledWith(3);
    });
    expect(showToastMock).toHaveBeenCalledWith("用户已删除", "success");
    expect(screen.queryByText("alice")).not.toBeInTheDocument();
  });
});
