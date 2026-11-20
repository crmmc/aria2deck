import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { UsersPage } from "@/features/users/UsersPage";
import {
  bytesToGib,
  formatQuotaForForm,
  gibToBytes,
  parseQuotaBytes,
} from "@/features/users/api";
import {
  machineSharePercent,
  storageBarSegments,
  toUserViews,
  usageColor,
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
) {
  mockFetch.mockReset();
  mockFetch.mockImplementation(async (url: string, init?: Init) => {
    if (url === "/api/v2/auth/me") return json(200, me);
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
  const cell = Array.from(document.querySelectorAll("td[data-label='用户名']")).find(
    (candidate) => candidate.textContent === username,
  );
  if (cell === undefined) throw new Error(`未找到用户名 ${username} 的行`);
  return cell.closest("tr") as HTMLElement;
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

describe("UsersPage 列表（V1 表格）", () => {
  it("renders the V1 table with badges, storage bar and machine share", async () => {
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

    // V1 表格类名与五列表头。
    const table = document.querySelector("table.table.text-left") as HTMLTableElement;
    expect(table).not.toBeNull();
    expect(document.querySelector(".users-table-wrapper")).not.toBeNull();
    const headers = Array.from(table.querySelectorAll("thead .table-cell")).map(
      (cell) => cell.textContent,
    );
    expect(headers).toEqual(["ID", "用户名", "角色", "存储", "操作"]);

    // V1 徽标：管理员 active，普通用户无修饰。
    expect(within(rowOf("admin")).getByText("管理员").className).toBe("badge active");
    expect(within(rowOf("alice")).getByText("用户").className).toBe("badge");

    // V1 存储列文本：已用 / 配额、百分比、可用与全站占比（分母 = 51 GiB）。
    const aliceRow = rowOf("alice");
    expect(aliceRow.textContent).toContain("50.0 GB / 100.0 GB");
    expect(aliceRow.textContent).toContain("50.0%");
    expect(aliceRow.textContent).toContain("可用 40.0 GB");
    expect(aliceRow.textContent).toContain("冻结 10.0 GB");
    expect(aliceRow.textContent).toContain("全站 98.0%");
    expect(aliceRow.querySelector(".user-storage-cell")).not.toBeNull();
    expect(aliceRow.querySelector(".user-storage-bar")).not.toBeNull();
    expect(aliceRow.querySelector(".user-storage-reserved")).not.toBeNull();
    expect(aliceRow.querySelector(".user-storage-sub")?.textContent).toContain("全站");
  });

  it("renders the storage bar widths and survives a zero quota", async () => {
    setup(() =>
      list([
        ME,
        // page_points: 85% 触发 warning 色，95% 触发 danger 色（V1 getUsageColor）。
        user(2, { username: "warn", used_bytes: 85, quota_bytes: 100, available_bytes: 15 }),
        user(3, { username: "danger", used_bytes: 95, quota_bytes: 100, available_bytes: 5 }),
        // quota 为 0：分段宽度按 V1 兜底为 0，不产生 NaN。
        user(4, { username: "noquota", used_bytes: 0, quota_bytes: 0, available_bytes: 0 }),
      ]),
    );
    await screen.findByText("warn");
    // jsdom 丢弃 var() 形式的 inline 背景色，颜色阈值由 usageColor 纯规则断言。
    const styleOf = (username: string) =>
      (rowOf(username).querySelector(".progress-bar") as HTMLElement).getAttribute("style") ?? "";
    expect(styleOf("warn")).toContain("width: 85%");
    expect(styleOf("danger")).toContain("width: 95%");
    expect(styleOf("noquota")).toContain("width: 0%");
    expect(rowOf("noquota").textContent).toContain("0.0%");
  });

  it("hides the delete button on the current admin row and keeps edit", async () => {
    setup(() => list([ME, user(2)]));
    await screen.findByText("admin");
    const selfRow = rowOf("admin");
    expect(within(selfRow).getByRole("button", { name: "编辑" })).toBeInTheDocument();
    expect(within(selfRow).queryByRole("button", { name: "删除" })).toBeNull();
    expect(within(rowOf("user-2")).getByRole("button", { name: "删除" })).toBeInTheDocument();
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

describe("UsersPage 创建用户（V1 表单）", () => {
  it("creates a user with hashed password, initial-password marker and quota bytes", async () => {
    setup((url, init) => {
      if (init?.method === "POST" && url === "/api/v2/users") {
        return user(9, { username: "bob", is_initial_password: true });
      }
      return list([ME]);
    });
    await screen.findByText("admin");

    const form = document.querySelector("form.create-user-form") as HTMLFormElement;
    expect(document.querySelector(".create-user-fields")).not.toBeNull();
    fireEvent.change(within(form).getByLabelText("用户名"), { target: { value: "bob" } });
    fireEvent.change(within(form).getByLabelText("密码"), { target: { value: "secret123" } });
    fireEvent.change(within(form).getByLabelText("存储配额"), { target: { value: "2" } });
    fireEvent.change(within(form).getByLabelText("配额单位"), { target: { value: "MB" } });
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

    // 成功后表单回到初始值。
    expect(within(form).getByLabelText("用户名")).toHaveValue("");
    expect(within(form).getByLabelText("密码")).toHaveValue("");
    expect(within(form).getByLabelText("存储配额")).toHaveValue(100);
    expect(within(form).getByLabelText("配额单位")).toHaveValue("GB");
    expect(within(form).getByLabelText("管理员")).not.toBeChecked();
    expect(await screen.findByText("bob")).toBeInTheDocument();
  });

  it("rejects a non-positive quota before sending anything", async () => {
    setup(() => list([ME]));
    await screen.findByText("admin");
    const form = document.querySelector("form.create-user-form") as HTMLFormElement;
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
    const form = document.querySelector("form.create-user-form") as HTMLFormElement;
    fireEvent.change(within(form).getByLabelText("用户名"), { target: { value: "bob" } });
    fireEvent.change(within(form).getByLabelText("密码"), { target: { value: "secret123" } });
    fireEvent.submit(form);
    expect(await screen.findByText("用户名已存在", undefined, SLOW)).toBeInTheDocument();
    expect(document.querySelector(".text-danger.mt-3")).not.toBeNull();
  });
});

describe("UsersPage 编辑用户（V1 弹窗 + V2 局部更新）", () => {
  function openEdit(username: string) {
    fireEvent.click(within(rowOf(username)).getByRole("button", { name: "编辑" }));
    return screen.getByLabelText("编辑用户");
  }

  it("shows the V1 occupancy summary and disables self role edit", async () => {
    setup(() => list([ME, user(2, { username: "alice", used_bytes: 50 * GIB, reserved_bytes: 10 * GIB, available_bytes: 40 * GIB })]));
    await screen.findByText("alice");
    const dialog = openEdit("alice");
    expect(dialog.textContent).toContain("当前占用：50.0 GB 已用");
    expect(dialog.textContent).toContain("· 10.0 GB 冻结");
    expect(dialog.textContent).toContain("· 可用 40.0 GB");
    expect(dialog.textContent).toContain("· 全站 98.0%");
    // V1 原文：编辑中另一个用户时角色复选框可改。
    expect(within(dialog).getByLabelText("管理员用户")).not.toBeDisabled();
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));
    expect(screen.queryByLabelText("编辑用户")).toBeNull();

    const selfDialog = openEdit("admin");
    const selfToggle = within(selfDialog).getByLabelText("管理员用户") as HTMLInputElement;
    expect(selfToggle.disabled).toBe(true);
    expect(selfDialog.textContent).toContain("(不能修改自己的角色)");
    fireEvent.click(within(selfDialog).getByRole("button", { name: "取消" }));
  });

  it("sends only the changed quota and never the initial-password field", async () => {
    setup((url, init) => {
      if (init?.method === "PATCH") return user(2, { username: "alice", quota_bytes: 3 * GIB });
      return list([ME, user(2, { username: "alice", quota_bytes: GIB })]);
    });
    await screen.findByText("alice");
    const dialog = openEdit("alice");
    // V1 回填口径：1 GiB 显示为 1.00 GB。
    expect(within(dialog).getByLabelText("存储配额")).toHaveValue(1);
    fireEvent.change(within(dialog).getByLabelText("存储配额"), { target: { value: "3" } });
    // 单位切换回 GB（V1 原控件），验证换算走的是当前单位。
    fireEvent.change(within(dialog).getByLabelText("配额单位"), { target: { value: "GB" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存更改" }));

    await waitFor(() => {
      const patch = mockFetch.mock.calls.find(([, init]) => init?.method === "PATCH");
      expect(patch).toBeDefined();
      // 未改密码时绝不能带 is_initial_password（V2 会拒绝）。
      expect(JSON.parse(patch![1].body as string)).toEqual({ quota_bytes: 3 * GIB });
    });
    await waitFor(() => expect(screen.queryByLabelText("编辑用户")).toBeNull());
  });

  it("toggles the role without a password and keeps is_initial_password out", async () => {
    setup((url, init) => {
      if (init?.method === "PATCH") return user(2, { username: "alice", is_admin: true });
      return list([ME, user(2, { username: "alice" })]);
    });
    await screen.findByText("alice");
    const dialog = openEdit("alice");
    fireEvent.click(within(dialog).getByLabelText("管理员用户"));
    fireEvent.click(within(dialog).getByRole("button", { name: "保存更改" }));
    await waitFor(() => {
      const patch = mockFetch.mock.calls.find(([, init]) => init?.method === "PATCH");
      expect(JSON.parse(patch![1].body as string)).toEqual({ is_admin: true });
    });
  });

  it("closes without any request when nothing changed", async () => {
    setup(() => list([ME, user(2, { username: "alice" })]));
    await screen.findByText("alice");
    const dialog = openEdit("alice");
    fireEvent.click(within(dialog).getByRole("button", { name: "保存更改" }));
    await waitFor(() => expect(screen.queryByLabelText("编辑用户")).toBeNull());
    expect(mockFetch.mock.calls.find(([, init]) => init?.method === "PATCH")).toBeUndefined();
  });

  it("C07: renaming without a new password is refused locally with the V2 message", async () => {
    setup(() => list([ME, user(2, { username: "alice" })]));
    await screen.findByText("alice");
    const dialog = openEdit("alice");
    fireEvent.change(within(dialog).getByLabelText("用户名"), { target: { value: "alice-neu" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存更改" }));
    expect(await screen.findByText("修改用户名时必须同时提供新密码")).toBeInTheDocument();
    expect(mockFetch.mock.calls.find(([, init]) => init?.method === "PATCH")).toBeUndefined();
    expect(screen.getByLabelText("编辑用户")).toBeInTheDocument();
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
    fireEvent.click(within(dialog).getByRole("button", { name: "保存更改" }));

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
  });

  it("does not mark the initial password when an admin resets their own password", async () => {
    setup((url, init) => {
      if (init?.method === "PATCH") return { ...ME, is_initial_password: false };
      return list([ME, user(2)]);
    });
    await screen.findByText("admin");
    const dialog = openEdit("admin");
    fireEvent.change(within(dialog).getByLabelText("新密码"), { target: { value: "adminpass" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存更改" }));
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
    fireEvent.click(within(dialog).getByRole("button", { name: "保存更改" }));
    expect(await screen.findByText("用户名已被占用", undefined, SLOW)).toBeInTheDocument();
    expect(screen.getByLabelText("编辑用户")).toBeInTheDocument();
  });

  it("reports a vanished user instead of PATCHing", async () => {
    setup(() => list([ME, user(2, { username: "alice" })]));
    await screen.findByText("alice");
    const dialog = openEdit("alice");
    // 打开编辑后该用户被别处删除：列表里已无此人。
    fireEvent.click(within(rowOf("alice")).getByRole("button", { name: "删除" }));
    const deleteDialog = await screen.findByLabelText("删除用户");
    fireEvent.click(within(deleteDialog).getByRole("button", { name: "删除" }));
    await waitFor(() => expect(screen.queryByLabelText("删除用户")).toBeNull());

    fireEvent.click(within(dialog).getByRole("button", { name: "保存更改" }));
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

describe("UsersPage 删除用户（V1 弹窗 + V2 202 受理）", () => {
  it("confirms then reports acceptance, not physical deletion", async () => {
    setup((url, init) => {
      if (init?.method === "DELETE") return { ok: true, state: "pending", accepted: true };
      return list([ME, user(2, { username: "alice" })]);
    });
    await screen.findByText("alice");
    fireEvent.click(within(rowOf("alice")).getByRole("button", { name: "删除" }));
    const dialog = screen.getByLabelText("删除用户");
    expect(dialog.textContent).toContain("确定要删除用户 alice 吗？");
    expect(dialog.textContent).toContain("将删除该用户的所有下载任务记录");
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));
    expect(await screen.findByText("删除已受理，后台清理进行中")).toBeInTheDocument();
    expect(screen.queryByText("alice")).toBeNull();
  });

  it("keeps the dialog and surfaces the failure toast", async () => {
    setup((url, init) => {
      if (init?.method === "DELETE") {
        return json(409, { error: { code: "content_busy", message: "用户仍有未清理的任务、文件或打包" } });
      }
      return list([ME, user(2, { username: "alice" })]);
    });
    await screen.findByText("alice");
    fireEvent.click(within(rowOf("alice")).getByRole("button", { name: "删除" }));
    const dialog = screen.getByLabelText("删除用户");
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));
    expect(
      await screen.findByText("用户仍有未清理的任务、文件或打包"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("删除用户")).toBeInTheDocument();
    expect(rowOf("alice")).toBeInTheDocument();
  });

  it("does not request a delete when cancelled", async () => {
    setup(() => list([ME, user(2, { username: "alice" })]));
    await screen.findByText("alice");
    fireEvent.click(within(rowOf("alice")).getByRole("button", { name: "删除" }));
    fireEvent.click(within(screen.getByLabelText("删除用户")).getByRole("button", { name: "取消" }));
    await waitFor(() => expect(screen.queryByLabelText("删除用户")).toBeNull());
    expect(mockFetch.mock.calls.find(([, init]) => init?.method === "DELETE")).toBeUndefined();
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
    { bytes: 2 * GIB, value: "2.00", unit: "GB" },
    { bytes: 3 * MIB, value: "3.00", unit: "MB" },
    { bytes: 2048, value: "2.00", unit: "KB" },
  ])("formatQuotaForForm($bytes) = $value $unit", ({ bytes, value, unit }) => {
    expect(formatQuotaForForm(bytes)).toEqual({ quotaValue: value, quotaUnit: unit });
  });

  it("converts GiB to bytes and rejects invalid input", () => {
    expect(gibToBytes(1.5)).toBe(Math.round(1.5 * GIB));
    expect(gibToBytes(0)).toBe(0);
    expect(gibToBytes(-1)).toBeNull();
    expect(gibToBytes(Number.NaN)).toBeNull();
    expect(gibToBytes(Number.POSITIVE_INFINITY)).toBeNull();
  });

  it("rounds bytes to two-decimal GiB", () => {
    expect(bytesToGib(GIB)).toBe(1);
    expect(bytesToGib(1.5 * GIB)).toBe(1.5);
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

  it.each([
    { percent: 0, color: "var(--success)" },
    { percent: 69.9, color: "var(--success)" },
    { percent: 70, color: "var(--warning)" },
    { percent: 89.9, color: "var(--warning)" },
    { percent: 90, color: "var(--danger)" },
    { percent: 100, color: "var(--danger)" },
  ])("usageColor($percent) = $color", ({ percent, color }) => {
    expect(usageColor(percent)).toBe(color);
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
