import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { UsersPage } from "@/features/users/UsersPage";
import {
  formatQuotaForForm,
  gibToBytes,
  parseQuotaBytes,
} from "@/features/users/api";
import {
  machineSharePercent,
  storageBarSegments,
  toUserViews,
  usagePercent,
} from "@/features/users/model";
import type { UserResponse } from "@/shared/api/types";

const mockFetch = jest.fn();
global.fetch = mockFetch as unknown as typeof fetch;

const GIB = 1024 ** 3;
const MIB = 1024 ** 2;
/** 涉及密码派生的断言要多等：jsdom 下 PBKDF2 10k 轮是真实的 CPU 开销，默认 1s 在覆盖率采集时会超时。 */
const SLOW = { timeout: 5000 } as const;

const ME: UserResponse = {
  id: 1,
  username: "admin",
  is_admin: true,
  quota_bytes: 200 * GIB,
  is_initial_password: false,
  pending_delete: false,
  used_bytes: GIB,
  reserved_bytes: 0,
  available_bytes: 199 * GIB,
};

function user(id: number, overrides: Partial<UserResponse> = {}): UserResponse {
  return {
    id,
    username: `user-${id}`,
    is_admin: false,
    quota_bytes: 100 * GIB,
    is_initial_password: false,
    pending_delete: false,
    used_bytes: 0,
    reserved_bytes: 0,
    available_bytes: 100 * GIB,
    ...overrides,
  };
}

function list(items: UserResponse[], total = items.length): unknown {
  return { items, total, page: 1, page_size: 100 };
}

function json(status: number, body: unknown): unknown {
  return { status, ok: status >= 200 && status < 300, json: () => Promise.resolve(body) };
}

type Init = { method?: string; body?: string };

/** mock 只打 fetch 边界；Authorization/headers 由共享 client 负责，不在断言范围。 */
function setup(
  handler: (url: string, init?: Init) => unknown,
  me: UserResponse = ME,
  refreshedMe?: UserResponse,
) {
  let authRequests = 0;
  mockFetch.mockReset();
  mockFetch.mockImplementation(async (url: string, init?: Init) => {
    if (url === "/api/v2/auth/me") {
      authRequests += 1;
      return json(200, authRequests === 1 ? me : (refreshedMe ?? me));
    }
    if (url === "/api/v2/public/site-info") {
      return json(200, {
        site_title: "Aria2Deck",
        ws_reconnect_max_delay_seconds: 30,
        ws_reconnect_jitter_seconds: 1,
        ws_reconnect_factor: 2,
      });
    }
    const result = handler(url, init);
    if (
      typeof result === "object" &&
      result !== null &&
      typeof (result as { json?: unknown }).json === "function"
    ) {
      return result;
    }
    return json(200, result);
  });
  return render(
    <MemoryRouter initialEntries={["/users"]}>
      <AppProviders>
        <UsersPage />
      </AppProviders>
    </MemoryRouter>,
  );
}

/** 按 V1 表格「用户名」单元格定位行：弹窗文案里也会出现用户名，不能直接用 getByText。 */
function rowOf(username: string): HTMLElement {
  const name = Array.from(document.querySelectorAll(".admin-user-name strong")).find(
    (candidate) => candidate.textContent === username,
  );
  if (name === undefined) throw new Error(`未找到用户名 ${username} 的行`);
  return name.closest("tr") as HTMLElement;
}

function userListCalls(): Init[] {
  return mockFetch.mock.calls
    .filter(([url]) => typeof url === "string" && url.startsWith("/api/v2/users"))
    .map(([, init]) => (init ?? {}) as Init);
}

// jsdom 没有实现 <dialog>.showModal/close；V1 弹窗（ModalOverlay）依赖它。
let originalShowModal: HTMLDialogElement["showModal"] | undefined;
let originalClose: HTMLDialogElement["close"] | undefined;

beforeEach(() => {
  originalShowModal = HTMLDialogElement.prototype.showModal;
  originalClose = HTMLDialogElement.prototype.close;
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
    configurable: true,
    value: jest.fn(function showModal(this: HTMLDialogElement) {
      this.setAttribute("open", "");
    }),
  });
  Object.defineProperty(HTMLDialogElement.prototype, "close", {
    configurable: true,
    value: jest.fn(function close(this: HTMLDialogElement) {
      this.removeAttribute("open");
    }),
  });
});

afterEach(() => {
  document.querySelectorAll("dialog").forEach((dialog) => dialog.removeAttribute("open"));
  if (originalShowModal) {
    Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
      configurable: true,
      value: originalShowModal,
    });
  } else {
    Reflect.deleteProperty(HTMLDialogElement.prototype, "showModal");
  }
  if (originalClose) {
    Object.defineProperty(HTMLDialogElement.prototype, "close", {
      configurable: true,
      value: originalClose,
    });
  } else {
    Reflect.deleteProperty(HTMLDialogElement.prototype, "close");
  }
});

describe("UsersPage 列表（Demo 表格）", () => {
  it("renders the Demo table, current-account marker, storage meter and totals", async () => {
    setup(() =>
      list([
        ME,
        user(2, {
          username: "alice",
          used_bytes: 50 * GIB,
          reserved_bytes: 10 * GIB,
          available_bytes: 40 * GIB,
        }),
      ]),
    );
    expect(await screen.findByText("alice")).toBeInTheDocument();
    expect(screen.getByText("用户管理")).toBeInTheDocument();
    expect(screen.getByText("管理账号、角色与存储配额")).toBeInTheDocument();

    const table = document.querySelector("table.admin-table.admin-user-table") as HTMLTableElement;
    expect(table).not.toBeNull();
    expect(document.querySelector(".admin-table-panel")).not.toBeNull();
    const headers = Array.from(table.querySelectorAll("thead th")).map((cell) => cell.textContent);
    expect(headers).toEqual(["ID", "用户名", "角色", "存储用量 / 配额 / 可用 / 全站占比", "操作"]);

    expect(within(rowOf("admin")).getByText("当前账号")).toBeInTheDocument();
    expect(within(rowOf("admin")).getByText("管理员").className).toBe("status-badge status-active");
    expect(within(rowOf("alice")).getByText("普通用户").className).toBe("status-badge");

    const aliceRow = rowOf("alice");
    expect(aliceRow.textContent).toContain("50.0 GB / 100 GB");
    expect(aliceRow.textContent).toContain("可用 40.0 GB");
    expect(aliceRow.textContent).toContain("预留 10.0 GB");
    expect(aliceRow.textContent).toContain("全站 98.0%");
    expect(within(aliceRow).getByRole("meter", { name: "alice 配额使用 50.0%" })).toHaveAttribute("aria-valuenow", "50");
    expect(aliceRow.querySelector(".admin-quota-used")).not.toBeNull();
    expect(aliceRow.querySelector(".admin-quota-reserved")).not.toBeNull();

    const footer = document.querySelector(".admin-table-footer") as HTMLElement;
    expect(footer.textContent).toContain("共 2 个用户");
    expect(footer.textContent).toContain("已使用 51.0 GB · 配额合计 300 GB");
  });

  it("renders separate used and reserved widths and survives a zero quota", async () => {
    setup(() =>
      list([
        ME,
        user(2, { username: "warn", used_bytes: 85, quota_bytes: 100, available_bytes: 15 }),
        user(3, { username: "danger", used_bytes: 95, quota_bytes: 100, available_bytes: 5 }),
        user(4, { username: "noquota", used_bytes: 0, quota_bytes: 0, available_bytes: 0 }),
      ]),
    );
    await screen.findByText("warn");
    const styleOf = (username: string) =>
      (rowOf(username).querySelector(".admin-quota-used") as HTMLElement).getAttribute("style") ?? "";
    expect(styleOf("warn")).toContain("width: 85%");
    expect(styleOf("danger")).toContain("width: 95%");
    expect(styleOf("noquota")).toContain("width: 0%");
    expect(within(rowOf("noquota")).getByRole("meter", { name: "noquota 配额使用 0.0%" })).toHaveAttribute("aria-valuenow", "0");
  });

  it("hides the delete button on the current admin row and keeps edit", async () => {
    setup(() => list([ME, user(2)]));
    await screen.findByText("admin");
    const selfRow = rowOf("admin");
    expect(within(selfRow).getByRole("button", { name: "编辑用户 admin" })).toBeInTheDocument();
    expect(within(selfRow).queryByRole("button", { name: "删除用户 admin" })).toBeNull();
    expect(within(rowOf("user-2")).getByRole("button", { name: "删除用户 user-2" })).toBeInTheDocument();
  });

  it("renders an empty table with real zero totals", async () => {
    setup(() => list([]));
    expect(await screen.findByText("共 0 个用户")).toBeInTheDocument();
    expect(document.querySelectorAll(".admin-user-table tbody tr")).toHaveLength(0);
    expect(document.querySelector(".admin-table-footer")?.textContent).toContain("已使用 0 B · 配额合计 0 B");
  });

  it("aggregates every V2 page in order and drops pending-delete users", async () => {
    setup((url) => {
      if (url.includes("page=2")) {
        return list([user(101), user(102, { pending_delete: true })], 103);
      }
      if (url.includes("page=3")) return list([], 103);
      return list(
        Array.from({ length: 100 }, (_unused, index) => user(index + 1)),
        103,
      );
    });
    expect(await screen.findByText("user-101")).toBeInTheDocument();
    expect(screen.getByText("user-100")).toBeInTheDocument();
    // 后台清理中的账号按 V1 口径不进入列表。
    expect(screen.queryByText("user-102")).toBeNull();
    const urls = mockFetch.mock.calls
      .map(([url]) => url as string)
      .filter((url) => url.startsWith("/api/v2/users"));
    expect(urls.some((url) => url.includes("page=1") && url.includes("page_size=100"))).toBe(true);
    expect(urls.some((url) => url.includes("page=2"))).toBe(true);
  });

  it("shows the V1 load error card", async () => {
    setup(() => json(500, { error: { code: "internal", message: "后端炸了" } }));
    expect(await screen.findByText("后端炸了")).toBeInTheDocument();
    expect(document.querySelector(".card.text-danger")).not.toBeNull();
  });

  it("redirects non-admins to /tasks and never lists users", async () => {
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string) => {
      if (url === "/api/v2/auth/me") return json(200, { ...ME, is_admin: false });
      return json(200, list([]));
    });
    render(
      <MemoryRouter initialEntries={["/users"]}>
        <AppProviders>
          <Routes>
            <Route path="/users" element={<UsersPage />} />
            <Route path="/tasks" element={<div>任务页占位</div>} />
          </Routes>
        </AppProviders>
      </MemoryRouter>,
    );
    expect(await screen.findByText("任务页占位")).toBeInTheDocument();
    expect(
      mockFetch.mock.calls.find(([url]) => (url as string).startsWith("/api/v2/users")),
    ).toBeUndefined();
  });
});

describe("UsersPage 创建用户（Demo 表单）", () => {
  it("creates a user with hashed password, initial-password marker and quota bytes", async () => {
    setup((url, init) => {
      if (init?.method === "POST" && url === "/api/v2/users") {
        return user(9, { username: "bob", is_initial_password: true });
      }
      return list([ME]);
    });
    await screen.findByText("admin");

    const form = document.querySelector("#admin-user-create-form") as HTMLFormElement;
    expect(document.querySelector(".admin-user-create-grid")).not.toBeNull();
    expect(within(form).getByLabelText("存储配额")).toHaveValue(10);
    expect(within(form).queryByRole("alert")).toBeNull();
    fireEvent.change(within(form).getByLabelText("用户名"), { target: { value: "bob" } });
    fireEvent.change(within(form).getByLabelText("密码"), { target: { value: "secret123" } });
    fireEvent.change(within(form).getByLabelText("存储配额"), { target: { value: "2" } });
    fireEvent.change(within(form).getByLabelText("存储配额单位"), { target: { value: "MB" } });
    fireEvent.click(within(form).getByLabelText("管理员"));
    fireEvent.submit(form);

    await waitFor(() => {
      const post = mockFetch.mock.calls.find(
        ([url, init]) => init?.method === "POST" && url === "/api/v2/users",
      );
      expect(post).toBeDefined();
      expect(JSON.parse(post![1].body as string)).toEqual({
        username: "bob",
        // 独立基准：pbkdf2_hmac('sha256', b'secret123', sha256(b'bob').hexdigest().encode(), 10000)
        password: "ca95e432502136463589237a06357aa4c30db1f8be3e3a4a9cf6ac57df638337",
        is_admin: true,
        is_initial_password: true,
        quota_bytes: 2 * MIB,
      });
    }, SLOW);

    expect(within(form).getByLabelText("用户名")).toHaveValue("");
    expect(within(form).getByLabelText("密码")).toHaveValue("");
    expect(within(form).getByLabelText("存储配额")).toHaveValue(10);
    expect(within(form).getByLabelText("存储配额单位")).toHaveValue("GB");
    expect(within(form).getByLabelText("管理员")).not.toBeChecked();
    expect(await screen.findByText("bob")).toBeInTheDocument();
    expect(await screen.findByText("用户已创建")).toBeInTheDocument();
  });

  it("rejects missing credentials before sending anything", async () => {
    setup(() => list([ME]));
    await screen.findByText("admin");
    const form = document.querySelector("#admin-user-create-form") as HTMLFormElement;
    fireEvent.submit(form);
    expect(await within(form).findByRole("alert")).toHaveTextContent("请填写用户名和密码");
    expect(userListCalls().find((init) => init.method === "POST")).toBeUndefined();
  });

  it("rejects a non-positive quota before sending anything", async () => {
    setup(() => list([ME]));
    await screen.findByText("admin");
    const form = document.querySelector("#admin-user-create-form") as HTMLFormElement;
    fireEvent.change(within(form).getByLabelText("用户名"), { target: { value: "bob" } });
    fireEvent.change(within(form).getByLabelText("密码"), { target: { value: "secret123" } });
    fireEvent.change(within(form).getByLabelText("存储配额"), { target: { value: "0" } });
    fireEvent.submit(form);
    expect(await screen.findByText("配额必须为正数")).toBeInTheDocument();
    expect(userListCalls().find((init) => init.method === "POST")).toBeUndefined();
  });

  it("shows the backend conflict message inline", async () => {
    setup((url, init) => {
      if (init?.method === "POST" && url === "/api/v2/users") {
        return json(409, { error: { code: "duplicate_resource", message: "用户名已存在" } });
      }
      return list([ME]);
    });
    await screen.findByText("admin");
    const form = document.querySelector("#admin-user-create-form") as HTMLFormElement;
    fireEvent.change(within(form).getByLabelText("用户名"), { target: { value: "bob" } });
    fireEvent.change(within(form).getByLabelText("密码"), { target: { value: "secret123" } });
    fireEvent.submit(form);
    expect(await screen.findByText("用户名已存在", undefined, SLOW)).toBeInTheDocument();
    expect(document.querySelector(".admin-create-error")).not.toBeNull();
  });
});

describe("UsersPage 编辑用户（Demo 弹窗 + V2 局部更新）", () => {
  function openEdit(username: string) {
    fireEvent.click(within(rowOf(username)).getByRole("button", { name: `编辑用户 ${username}` }));
    return screen.getByRole("dialog", { name: `编辑用户 ${username}` });
  }

  it("shows the Demo occupancy summary and disables self role edit", async () => {
    setup(() => list([ME, user(2, { username: "alice", used_bytes: 50 * GIB, reserved_bytes: 10 * GIB, available_bytes: 40 * GIB })]));
    await screen.findByText("alice");
    const dialog = openEdit("alice");
    expect(dialog.textContent).toContain("编辑用户 alice");
    expect(dialog.textContent).toContain("当前已使用 50.0 GB，预留 10.0 GB，可用 40.0 GB。");
    const role = within(dialog).getByLabelText("角色") as HTMLSelectElement;
    expect(role).not.toBeDisabled();
    expect(role).toHaveValue("user");
    expect(within(dialog).getByLabelText("存储配额单位")).toHaveValue("GB");
    expect(within(dialog).getByLabelText("存储配额单位")).toHaveAttribute("readonly");
    expect(within(dialog).getByLabelText("存储配额")).toHaveAttribute("step", "any");
    expect(document.querySelectorAll("#admin-user-edit-form .field-note")).toHaveLength(4);
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("dialog")).toBeNull();

    const selfDialog = openEdit("admin");
    expect(within(selfDialog).getByLabelText("角色")).toBeDisabled();
    expect(selfDialog.textContent).toContain("当前账号的角色不可修改。");
    fireEvent.click(within(selfDialog).getByRole("button", { name: "取消" }));
  });

  it("focuses the username and restores the row action focus after closing", async () => {
    setup(() => list([ME, user(2, { username: "alice" })]));
    await screen.findByText("alice");
    const trigger = within(rowOf("alice")).getByRole("button", { name: "编辑用户 alice" });
    trigger.focus();
    fireEvent.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "编辑用户 alice" });
    expect(within(dialog).getByLabelText("用户名")).toHaveFocus();

    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(trigger).toHaveFocus();
  });

  it("sends only the changed quota and never the initial-password field", async () => {
    setup((url, init) => {
      if (init?.method === "PATCH") return user(2, { username: "alice", quota_bytes: 3 * GIB });
      return list([ME, user(2, { username: "alice", quota_bytes: GIB })]);
    });
    await screen.findByText("alice");
    const dialog = openEdit("alice");
    expect(within(dialog).getByLabelText("存储配额")).toHaveValue(1);
    fireEvent.change(within(dialog).getByLabelText("存储配额"), { target: { value: "3" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存修改" }));

    await waitFor(() => {
      const patch = mockFetch.mock.calls.find(([, init]) => init?.method === "PATCH");
      expect(patch).toBeDefined();
      expect(JSON.parse(patch![1].body as string)).toEqual({ quota_bytes: 3 * GIB });
    });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(await screen.findByText("用户信息已更新")).toBeInTheDocument();
  });

  it("changes the role without submitting an untouched fractional quota", async () => {
    const originalQuota = GIB + 1;
    setup((url, init) => {
      if (init?.method === "PATCH") return user(2, { username: "alice", is_admin: true, quota_bytes: originalQuota });
      return list([ME, user(2, { username: "alice", quota_bytes: originalQuota })]);
    });
    await screen.findByText("alice");
    const dialog = openEdit("alice");
    expect((within(dialog).getByLabelText("存储配额") as HTMLInputElement).value).toBe(String(originalQuota / GIB));
    fireEvent.change(within(dialog).getByLabelText("角色"), { target: { value: "admin" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存修改" }));
    await waitFor(() => {
      const patch = mockFetch.mock.calls.find(([, init]) => init?.method === "PATCH");
      expect(JSON.parse(patch![1].body as string)).toEqual({ is_admin: true });
    });
  });

  it.each([1, GIB + 1])("closes an untouched %i-byte quota without any PATCH", async (quotaBytes) => {
    setup(() => list([ME, user(2, { username: "alice", quota_bytes: quotaBytes })]));
    await screen.findByText("alice");
    const dialog = openEdit("alice");
    expect((within(dialog).getByLabelText("存储配额") as HTMLInputElement).value).toBe(String(quotaBytes / GIB));
    fireEvent.click(within(dialog).getByRole("button", { name: "保存修改" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(mockFetch.mock.calls.find(([, init]) => init?.method === "PATCH")).toBeUndefined();
  });

  it("rejects an empty username before sending anything", async () => {
    setup(() => list([ME, user(2, { username: "alice" })]));
    await screen.findByText("alice");
    const dialog = openEdit("alice");
    fireEvent.change(within(dialog).getByLabelText("用户名"), { target: { value: "" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存修改" }));
    expect(await screen.findByText("请输入用户名")).toBeInTheDocument();
    expect(mockFetch.mock.calls.find(([, init]) => init?.method === "PATCH")).toBeUndefined();
  });

  it("C07: renaming without a new password is refused locally with the V2 message", async () => {
    setup(() => list([ME, user(2, { username: "alice" })]));
    await screen.findByText("alice");
    const dialog = openEdit("alice");
    fireEvent.change(within(dialog).getByLabelText("用户名"), { target: { value: "alice-neu" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存修改" }));
    expect(await screen.findByText("修改用户名时必须同时提供新密码")).toBeInTheDocument();
    expect(mockFetch.mock.calls.find(([, init]) => init?.method === "PATCH")).toBeUndefined();
    expect(dialog).toBeInTheDocument();
  });

  it("renames with a new password derived from the new username and marks it initial", async () => {
    setup((url, init) => {
      if (init?.method === "PATCH") return user(2, { username: "alice-neu", is_initial_password: true });
      return list([ME, user(2, { username: "alice" })]);
    });
    await screen.findByText("alice");
    const dialog = openEdit("alice");
    fireEvent.change(within(dialog).getByLabelText("用户名"), { target: { value: "alice-neu" } });
    fireEvent.change(within(dialog).getByLabelText("新密码"), { target: { value: "newpass123" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存修改" }));

    await waitFor(() => {
      const patch = mockFetch.mock.calls.find(([, init]) => init?.method === "PATCH");
      expect(patch).toBeDefined();
      expect(JSON.parse(patch![1].body as string)).toEqual({
        username: "alice-neu",
        // 独立基准：盐 = sha256('alice-neu')，密码 'newpass123'。
        password: "c70a4f39acbecbe3ebb29528cd543184c72b9a3705a1dc771d80392aa2f6a946",
        // 管理员改他人密码 → 标记初始密码（V1 user_service.py:317）。
        is_initial_password: true,
      });
    }, SLOW);
    expect(await screen.findByText("alice-neu")).toBeInTheDocument();
    expect(mockFetch.mock.calls.filter(([url]) => url === "/api/v2/auth/me")).toHaveLength(1);
  });

  it("refreshes auth after the current admin renames themselves", async () => {
    const renamed = { ...ME, username: "admin-new" };
    setup((url, init) => {
      if (init?.method === "PATCH") return renamed;
      return list([ME, user(2)]);
    }, ME, renamed);
    await screen.findByText("admin");
    const dialog = openEdit("admin");
    fireEvent.change(within(dialog).getByLabelText("用户名"), { target: { value: "admin-new" } });
    fireEvent.change(within(dialog).getByLabelText("新密码"), { target: { value: "adminpass" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存修改" }));

    await waitFor(() => {
      expect(mockFetch.mock.calls.filter(([url]) => url === "/api/v2/auth/me")).toHaveLength(2);
      expect(screen.queryByRole("dialog")).toBeNull();
    }, SLOW);
    const patch = mockFetch.mock.calls.find(([, init]) => init?.method === "PATCH");
    expect(JSON.parse(patch![1].body as string)).toEqual({
      username: "admin-new",
      password: expect.any(String),
      is_initial_password: false,
    });
    expect(await screen.findByText("admin-new")).toBeInTheDocument();
    expect(await screen.findByText("用户信息已更新")).toBeInTheDocument();
  });

  it("does not mark the initial password when an admin resets their own password", async () => {
    setup((url, init) => {
      if (init?.method === "PATCH") return { ...ME, is_initial_password: false };
      return list([ME, user(2)]);
    });
    await screen.findByText("admin");
    const dialog = openEdit("admin");
    fireEvent.change(within(dialog).getByLabelText("新密码"), { target: { value: "adminpass" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存修改" }));
    await waitFor(() => {
      const patch = mockFetch.mock.calls.find(([, init]) => init?.method === "PATCH");
      expect(JSON.parse(patch![1].body as string)).toEqual({
        password: "80d0b0f3f7511f36a881829823a247ae270d836a11294fa028ef74dedc37e748",
        is_initial_password: false,
      });
    }, SLOW);
  });

  it("keeps the dialog open with the backend message on a rejected PATCH", async () => {
    setup((url, init) => {
      if (init?.method === "PATCH") {
        return json(409, { error: { code: "duplicate_resource", message: "用户名已被占用" } });
      }
      return list([ME, user(2, { username: "alice" })]);
    });
    await screen.findByText("alice");
    const dialog = openEdit("alice");
    fireEvent.change(within(dialog).getByLabelText("用户名"), { target: { value: "bob" } });
    fireEvent.change(within(dialog).getByLabelText("新密码"), { target: { value: "newpass123" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存修改" }));
    expect(await screen.findByText("用户名已被占用", undefined, SLOW)).toBeInTheDocument();
    expect(dialog).toBeInTheDocument();
  });

  it("reports a vanished user instead of PATCHing", async () => {
    setup((url, init) => {
      if (init?.method === "DELETE") return json(202, { ok: true, state: "pending", accepted: true });
      return list([ME, user(2, { username: "alice" })]);
    });
    await screen.findByText("alice");
    const dialog = openEdit("alice");
    // 打开编辑后该用户被别处删除：列表里已无此人。
    fireEvent.click(within(rowOf("alice")).getByRole("button", { name: "删除用户 alice" }));
    const deleteDialog = await screen.findByLabelText("删除用户");
    fireEvent.click(within(deleteDialog).getByRole("button", { name: "删除用户" }));
    await waitFor(() => expect(screen.queryByLabelText("删除用户")).toBeNull());

    fireEvent.click(within(dialog).getByRole("button", { name: "保存修改" }));
    expect(await screen.findByText("用户不存在或已被删除")).toBeInTheDocument();
    expect(mockFetch.mock.calls.find(([, init]) => init?.method === "PATCH")).toBeUndefined();
  });

  it("rejects a non-positive quota in the edit dialog", async () => {
    setup(() => list([ME, user(2, { username: "alice" })]));
    await screen.findByText("alice");
    const dialog = openEdit("alice");
    fireEvent.change(within(dialog).getByLabelText("存储配额"), { target: { value: "0" } });
    fireEvent.submit(dialog.querySelector("form") as HTMLFormElement);
    expect(await screen.findByText("配额必须为正数")).toBeInTheDocument();
    expect(mockFetch.mock.calls.find(([, init]) => init?.method === "PATCH")).toBeUndefined();
  });
});

describe("UsersPage 删除用户（Demo 弹窗 + V2 202 受理）", () => {
  it("confirms then reports acceptance, not physical deletion", async () => {
    setup((url, init) => {
      if (init?.method === "DELETE") return json(202, { ok: true, state: "pending", accepted: true });
      return list([ME, user(2, { username: "alice" })]);
    });
    await screen.findByText("alice");
    fireEvent.click(within(rowOf("alice")).getByRole("button", { name: "删除用户 alice" }));
    const dialog = screen.getByRole("dialog", { name: "删除用户" });
    expect(dialog.textContent).toContain("确定删除“alice”吗？其文件会在后台继续清理。");
    expect(within(dialog).getByRole("button", { name: "返回" })).toHaveFocus();
    fireEvent.click(within(dialog).getByRole("button", { name: "删除用户" }));
    expect(await screen.findByText("删除已受理，后台清理进行中")).toBeInTheDocument();
    expect(screen.queryByText("alice")).toBeNull();
  });

  it("keeps the busy delete modal open and prevents duplicate requests", async () => {
    let resolveDelete!: (value: unknown) => void;
    const pendingDelete = new Promise((resolve) => { resolveDelete = resolve; });
    setup((url, init) => {
      if (init?.method === "DELETE") return pendingDelete;
      return list([ME, user(2, { username: "alice" })]);
    });
    await screen.findByText("alice");
    fireEvent.click(within(rowOf("alice")).getByRole("button", { name: "删除用户 alice" }));
    const dialog = screen.getByRole("dialog", { name: "删除用户" });
    fireEvent.click(within(dialog).getByRole("button", { name: "删除用户" }));

    const busyButton = await within(dialog).findByRole("button", { name: "删除中..." });
    expect(busyButton).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "返回" })).toBeDisabled();
    expect(within(dialog).getByRole("button", { name: "关闭对话框" })).toBeDisabled();
    fireEvent.click(busyButton);
    expect(mockFetch.mock.calls.filter(([, init]) => init?.method === "DELETE")).toHaveLength(1);

    resolveDelete(json(202, { ok: true, state: "pending", accepted: true }));
    expect(await screen.findByText("删除已受理，后台清理进行中")).toBeInTheDocument();
  });

  it("keeps the dialog and surfaces the failure toast", async () => {
    setup((url, init) => {
      if (init?.method === "DELETE") {
        return json(409, { error: { code: "content_busy", message: "用户仍有未清理的任务、文件或打包" } });
      }
      return list([ME, user(2, { username: "alice" })]);
    });
    await screen.findByText("alice");
    fireEvent.click(within(rowOf("alice")).getByRole("button", { name: "删除用户 alice" }));
    const dialog = screen.getByRole("dialog", { name: "删除用户" });
    fireEvent.click(within(dialog).getByRole("button", { name: "删除用户" }));
    expect(
      await screen.findByText("用户仍有未清理的任务、文件或打包"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("删除用户")).toBeInTheDocument();
    expect(rowOf("alice")).toBeInTheDocument();
  });

  it("does not request a delete when cancelled", async () => {
    setup(() => list([ME, user(2, { username: "alice" })]));
    await screen.findByText("alice");
    fireEvent.click(within(rowOf("alice")).getByRole("button", { name: "删除用户 alice" }));
    fireEvent.click(within(screen.getByLabelText("删除用户")).getByRole("button", { name: "返回" }));
    await waitFor(() => expect(screen.queryByLabelText("删除用户")).toBeNull());
    expect(mockFetch.mock.calls.find(([, init]) => init?.method === "DELETE")).toBeUndefined();
  });
});

describe("UsersPage 移动卡片布局", () => {
  const originalWidth = window.innerWidth;

  function setViewportWidth(width: number) {
    Object.defineProperty(window, "innerWidth", { value: width, configurable: true });
    window.dispatchEvent(new Event("resize"));
  }

  beforeEach(() => {
    setViewportWidth(390);
  });

  afterEach(() => {
    // 只还原宽度、不再派发 resize：上一个测试的组件此刻尚未卸载，派发会触发 act 警告。
    Object.defineProperty(window, "innerWidth", { value: originalWidth, configurable: true });
  });

  it("用卡片展示用户名、角色、配额用量与进度条，不再渲染表格", async () => {
    setup(() =>
      list([
        ME,
        user(2, {
          username: "alice",
          used_bytes: 50 * GIB,
          reserved_bytes: 10 * GIB,
          available_bytes: 40 * GIB,
        }),
      ]),
    );
    const aliceCard = await screen.findByTestId("user-card-2");
    expect(document.querySelector("table")).toBeNull();
    expect(document.querySelectorAll(".user-mobile-card")).toHaveLength(2);

    expect(within(aliceCard).getByText("alice")).toBeInTheDocument();
    expect(within(aliceCard).getByText("普通用户").className).toBe("status-badge");
    expect(aliceCard.textContent).toContain("50.0 GB / 100 GB");
    expect(aliceCard.textContent).toContain("可用 40.0 GB");
    expect(aliceCard.textContent).toContain("预留 10.0 GB");
    expect(aliceCard.textContent).toContain("全站 98.0%");
    expect(within(aliceCard).getByRole("meter", { name: "alice 配额使用 50.0%" }))
      .toHaveAttribute("aria-valuenow", "50");
    expect(aliceCard.querySelector(".admin-quota-used")).not.toBeNull();
    expect(aliceCard.querySelector(".admin-quota-reserved")).not.toBeNull();
    expect(within(aliceCard).getByRole("button", { name: "编辑用户 alice" })).toBeInTheDocument();
    expect(within(aliceCard).getByRole("button", { name: "删除用户 alice" })).toBeInTheDocument();

    const selfCard = screen.getByTestId("user-card-1");
    expect(within(selfCard).getByText("当前账号")).toBeInTheDocument();
    expect(within(selfCard).getByText("管理员").className).toBe("status-badge status-active");
    expect(within(selfCard).queryByRole("button", { name: "删除用户 admin" })).toBeNull();

    const footer = document.querySelector(".admin-table-footer") as HTMLElement;
    expect(footer.textContent).toContain("共 2 个用户");
    expect(footer.textContent).toContain("已使用 51.0 GB · 配额合计 300 GB");
  });

  it("卡片上的编辑与删除按钮走与桌面一致的既有流程", async () => {
    setup((url, init) => {
      if (init?.method === "DELETE") return json(202, { ok: true, state: "pending", accepted: true });
      return list([ME, user(2, { username: "alice" })]);
    });
    const aliceCard = await screen.findByTestId("user-card-2");

    fireEvent.click(within(aliceCard).getByRole("button", { name: "编辑用户 alice" }));
    const editDialog = screen.getByRole("dialog", { name: "编辑用户 alice" });
    expect(editDialog.textContent).toContain("编辑用户 alice");
    fireEvent.click(within(editDialog).getByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    fireEvent.click(within(aliceCard).getByRole("button", { name: "删除用户 alice" }));
    const deleteDialog = screen.getByRole("dialog", { name: "删除用户" });
    fireEvent.click(within(deleteDialog).getByRole("button", { name: "删除用户" }));
    expect(await screen.findByText("删除已受理，后台清理进行中")).toBeInTheDocument();
    expect(mockFetch.mock.calls.filter(([, init]) => init?.method === "DELETE")).toHaveLength(1);
  });
});

describe("users 配额与占比口径", () => {
  it.each([
    { value: "2", unit: "MB", expected: 2 * MIB },
    { value: "1.5", unit: "GB", expected: Math.round(1.5 * GIB) },
    { value: "512", unit: "KB", expected: 512 * 1024 },
    { value: "1.0000000001", unit: "KB", expected: 1024 },
  ])("parseQuotaBytes($value $unit) = $expected", ({ value, unit, expected }) => {
    expect(parseQuotaBytes(value, unit)).toBe(expected);
  });

  it.each([
    { value: "0", unit: "GB" },
    { value: "-1", unit: "GB" },
    { value: "abc", unit: "GB" },
    { value: "1", unit: "TB" },
  ])("parseQuotaBytes rejects $value $unit", ({ value, unit }) => {
    expect(parseQuotaBytes(value, unit)).toBeNull();
  });

  it.each([
    { bytes: 2 * GIB, value: "2" },
    { bytes: 3 * MIB, value: "0.0029296875" },
    { bytes: 2048, value: "0.0000019073486328125" },
    { bytes: 1, value: "9.313225746154785e-10" },
  ])("formatQuotaForForm($bytes) keeps the exact GB string", ({ bytes, value }) => {
    expect(formatQuotaForForm(bytes)).toEqual({ quotaValue: value, quotaUnit: "GB" });
  });

  it("converts GiB to bytes and rejects invalid input", () => {
    expect(gibToBytes(1.5)).toBe(Math.round(1.5 * GIB));
    expect(gibToBytes(0)).toBe(0);
    expect(gibToBytes(-1)).toBeNull();
    expect(gibToBytes(Number.NaN)).toBeNull();
    expect(gibToBytes(Number.POSITIVE_INFINITY)).toBeNull();
  });

  it("computes usage and machine share with the V1 formula, clamped to 0..100", () => {
    expect(usagePercent(50, 200)).toBe(25);
    expect(usagePercent(1, 0)).toBe(0);
    expect(usagePercent(-5, 100)).toBe(0);
    expect(usagePercent(500, 100)).toBe(100);
    expect(machineSharePercent(1, 0)).toBe(0);
    expect(machineSharePercent(1, 3)).toBe(33.3);
    expect(machineSharePercent(9, 3)).toBe(100);
  });

  it("clamps the storage bar segments to the quota", () => {
    expect(storageBarSegments(user(1, { used_bytes: 25, quota_bytes: 100, reserved_bytes: 25 }))).toEqual({
      usedPct: 25,
      reservedPct: 25,
    });
    // 冻结部分不能超过剩余宽度。
    expect(storageBarSegments(user(1, { used_bytes: 90, quota_bytes: 100, reserved_bytes: 50 }))).toEqual({
      usedPct: 90,
      reservedPct: 10,
    });
    expect(storageBarSegments(user(1, { used_bytes: 0, quota_bytes: 0, reserved_bytes: 0 }))).toEqual({
      usedPct: 0,
      reservedPct: 0,
    });
  });

  it("derives views from the whole list so the machine-share denominator is the total used", () => {
    const views = toUserViews([
      user(1, { used_bytes: 10, quota_bytes: 100 }),
      user(2, { used_bytes: 30, quota_bytes: 0 }),
    ]);
    expect(views[0].usage_percent).toBe(10);
    expect(views[0].machine_share_percent).toBe(25);
    // quota 为 0 时按 V1 公式占比为 0，但机器占比仍按全量分母计算。
    expect(views[1].usage_percent).toBe(0);
    expect(views[1].machine_share_percent).toBe(75);
  });
});
