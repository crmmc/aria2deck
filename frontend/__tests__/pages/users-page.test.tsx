import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
  used_bytes: 100 * 1024 * 1024,
  reserved_bytes: 0,
  available_bytes: 924 * 1024 * 1024,
  usage_percent: 9.8,
  machine_share_percent: 20,
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
  used_bytes: 400 * 1024 * 1024,
  reserved_bytes: 50 * 1024 * 1024,
  available_bytes: 1600 * 1024 * 1024,
  usage_percent: 19.5,
  machine_share_percent: 80,
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

  test("renders storage usage and machine share in the users table", async () => {
    await renderAdminPage();

    expect(screen.getByText("存储")).toBeInTheDocument();
    expect(screen.getByText(/400.0 MB \/ 2.0 GB/)).toBeInTheDocument();
    expect(screen.getByText(/全站 80.0%/)).toBeInTheDocument();
  });

  test("shows readonly occupancy summary when editing a user", async () => {
    await renderAdminPage();

    const managedRow = screen.getByText("alice").closest("tr") as HTMLElement;
    fireEvent.click(within(managedRow).getByRole("button", { name: "编辑" }));

    expect(await screen.findByText(/当前占用：400.0 MB 已用/)).toBeInTheDocument();
    const dialog = screen.getByLabelText("编辑用户");
    expect(within(dialog).getByText(/全站 80.0%/)).toBeInTheDocument();
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

  test("shows create form error when creating a user fails", async () => {
    mockApi.createUser.mockRejectedValue(new Error("用户名已存在") as never);
    const { container } = await renderAdminPage();
    const { form, usernameInput, passwordInput } = getCreateForm(container);

    fireEvent.change(usernameInput, { target: { value: "dup-user" } });
    fireEvent.change(passwordInput, { target: { value: "pass123456" } });
    fireEvent.submit(form);

    expect(await screen.findByText("用户名已存在")).toBeInTheDocument();
  });

  async function openAliceEditModal() {
    await renderAdminPage();
    const managedRow = screen.getByText("alice").closest("tr") as HTMLElement;
    fireEvent.click(within(managedRow).getByRole("button", { name: "编辑" }));
    const modal = await screen.findByText("编辑用户");
    return modal.closest(".modal-content") as HTMLElement;
  }

  function getEditInputs(modalContent: HTMLElement) {
    const usernameInput = within(modalContent).getByDisplayValue("alice");
    const passwordInput = modalContent.querySelector(
      "input[type='password']",
    ) as HTMLInputElement;
    const quotaInput = modalContent.querySelector(
      "input[type='number']",
    ) as HTMLInputElement;
    return { usernameInput, passwordInput, quotaInput };
  }

  test("closes the edit modal without api call when nothing changed", async () => {
    const modalContent = await openAliceEditModal();

    fireEvent.click(within(modalContent).getByRole("button", { name: "保存更改" }));

    await waitFor(() => {
      expect(screen.queryByText("编辑用户")).not.toBeInTheDocument();
    });
    expect(mockApi.updateUser).not.toHaveBeenCalled();
  });

  test("closes the edit dialog via the cancel button", async () => {
    const modalContent = await openAliceEditModal();

    fireEvent.click(within(modalContent).getByRole("button", { name: "取消" }));

    await waitFor(() => {
      expect(screen.queryByText("编辑用户")).not.toBeInTheDocument();
    });
  });

  test("closes the delete dialog via the cancel button", async () => {
    await renderAdminPage();
    const managedRow = screen.getByText("alice").closest("tr") as HTMLElement;
    fireEvent.click(within(managedRow).getByRole("button", { name: "删除" }));

    const modal = await screen.findByText("删除用户");
    const modalContent = modal.closest(".modal-content") as HTMLElement;
    fireEvent.click(within(modalContent).getByRole("button", { name: "取消" }));

    await waitFor(() => {
      expect(screen.queryByText("删除用户")).not.toBeInTheDocument();
    });
    expect(mockApi.deleteUser).not.toHaveBeenCalled();
    expect(screen.getByText("alice")).toBeInTheDocument();
  });

  test("shows edit error when quota in edit modal is invalid", async () => {
    const modalContent = await openAliceEditModal();
    const { quotaInput } = getEditInputs(modalContent);
    const form = modalContent.querySelector("form") as HTMLFormElement;

    fireEvent.change(quotaInput, { target: { value: "0" } });
    fireEvent.submit(form);

    expect(await screen.findByText("配额必须为正数")).toBeInTheDocument();
    expect(mockApi.updateUser).not.toHaveBeenCalled();
    expect(screen.getByText("编辑用户")).toBeInTheDocument();
  });

  test("shows edit error when updating a user fails", async () => {
    mockApi.updateUser.mockRejectedValue(new Error("更新失败") as never);
    const modalContent = await openAliceEditModal();
    const { usernameInput } = getEditInputs(modalContent);

    fireEvent.change(usernameInput, { target: { value: "alice-updated" } });
    fireEvent.click(within(modalContent).getByRole("button", { name: "保存更改" }));

    expect(await screen.findByText("更新失败")).toBeInTheDocument();
    expect(screen.getByText("编辑用户")).toBeInTheDocument();
  });

  test("sends only the changed username when password and quota are untouched", async () => {
    mockApi.updateUser.mockResolvedValue({ ...managedUser, username: "alice-renamed" } as never);
    const modalContent = await openAliceEditModal();
    const { usernameInput } = getEditInputs(modalContent);

    fireEvent.change(usernameInput, { target: { value: "alice-renamed" } });
    fireEvent.click(within(modalContent).getByRole("button", { name: "保存更改" }));

    await waitFor(() => {
      expect(mockApi.updateUser).toHaveBeenCalledWith(3, { username: "alice-renamed" }, "alice");
    });
    expect(screen.queryByText("编辑用户")).not.toBeInTheDocument();
    expect(await screen.findByText("alice-renamed")).toBeInTheDocument();
  });

  test("rejects the edit submit when the user was deleted while editing", async () => {
    mockApi.deleteUser.mockResolvedValue({ ok: true } as never);
    const modalContent = await openAliceEditModal();

    const managedRow = screen.getByText("alice").closest("tr") as HTMLElement;
    fireEvent.click(within(managedRow).getByRole("button", { name: "删除" }));
    const deleteModal = await screen.findByText("删除用户");
    const deleteModalContent = deleteModal.closest(".modal-content") as HTMLElement;
    fireEvent.click(within(deleteModalContent).getByRole("button", { name: "删除" }));

    await waitFor(() => {
      expect(mockApi.deleteUser).toHaveBeenCalledWith(3);
    });

    fireEvent.click(within(modalContent).getByRole("button", { name: "保存更改" }));

    expect(await screen.findByText("用户不存在或已被删除")).toBeInTheDocument();
    expect(mockApi.updateUser).not.toHaveBeenCalled();
  });

  test("shows error toast when deleting a user fails", async () => {
    mockApi.deleteUser.mockRejectedValue(new Error("delete failed") as never);
    await renderAdminPage();

    const managedRow = screen.getByText("alice").closest("tr") as HTMLElement;
    fireEvent.click(within(managedRow).getByRole("button", { name: "删除" }));

    const modal = await screen.findByText("删除用户");
    const modalContent = modal.closest(".modal-content") as HTMLElement;
    fireEvent.click(within(modalContent).getByRole("button", { name: "删除" }));

    await waitFor(() => {
      expect(showToastMock).toHaveBeenCalledWith("删除用户失败", "error");
    });
    expect(screen.getByText("删除用户")).toBeInTheDocument();
  });

  test.each([
    {
      name: "me resolves after unmount",
      unmountBeforeMe: true,
      rejectList: false,
    },
    {
      name: "list users resolves after unmount",
      unmountBeforeMe: false,
      rejectList: false,
    },
    {
      name: "initial load rejects after unmount",
      unmountBeforeMe: false,
      rejectList: true,
    },
  ])("ignores initial load responses after unmount ($name)", async ({ unmountBeforeMe, rejectList }) => {
    let resolveMe: (value: typeof adminUser) => void = () => {};
    let settleList: () => void = () => {};
    mockApi.me.mockReturnValue(
      new Promise((done) => {
        resolveMe = done;
      }) as never,
    );
    mockApi.listUsers.mockReturnValue(
      new Promise((done, rej) => {
        settleList = () => (rejectList ? rej(new Error("late failure")) : done([adminUser, managedUser]));
      }) as never,
    );

    const { unmount } = renderPage();
    if (unmountBeforeMe) {
      unmount();
      await act(async () => {
        resolveMe(adminUser);
        await new Promise((done) => setTimeout(done, 0));
      });
      return;
    }

    await act(async () => {
      resolveMe(adminUser);
      await new Promise((done) => setTimeout(done, 0));
    });
    unmount();

    await act(async () => {
      settleList();
      await new Promise((done) => setTimeout(done, 0));
    });
    expect(pushMock).not.toHaveBeenCalled();
  });
});
