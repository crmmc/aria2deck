import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { UsersPage } from "@/features/users/UsersPage";
import type { UserResponse } from "@/shared/api/types";

const mockFetch = jest.fn();
global.fetch = mockFetch as unknown as typeof fetch;

const ME: UserResponse = {
  id: 1,
  username: "admin",
  is_admin: true,
  quota_bytes: 200 * 1024 ** 3,
  is_initial_password: false,
  pending_delete: false,
  used_bytes: 1024,
  reserved_bytes: 2048,
  available_bytes: 100,
};

function user(id: number, overrides: Partial<UserResponse> = {}): UserResponse {
  return {
    id,
    username: `user-${id}`,
    is_admin: false,
    quota_bytes: 100 * 1024 ** 3,
    is_initial_password: false,
    pending_delete: false,
    used_bytes: 0,
    reserved_bytes: 0,
    available_bytes: 100 * 1024 ** 3,
    ...overrides,
  };
}

function list(items: UserResponse[], total = items.length): unknown {
  return { items, total, page: 1, page_size: 20 };
}

function json(status: number, body: unknown): unknown {
  return { status, ok: status >= 200 && status < 300, json: () => Promise.resolve(body) };
}

function setup(
  handler: (url: string, init?: { method?: string; body?: string }) => unknown,
  me: UserResponse = ME,
) {
  mockFetch.mockReset();
  mockFetch.mockImplementation(async (url: string, init?: { method?: string; body?: string }) => {
    if (url === "/api/v2/auth/me") return json(200, me);
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

const GIB = 1024 ** 3;

describe("UsersPage list", () => {
  it("renders users with quota, usage and badges", async () => {
    setup(() =>
      list([
        ME,
        user(2, { pending_delete: true, is_initial_password: true, used_bytes: 5 * GIB, reserved_bytes: 2 * GIB }),
      ]),
    );
    expect(await screen.findByTestId("user-row-1")).toBeInTheDocument();
    expect(screen.getByText("管理员")).toBeInTheDocument();
    expect(screen.getByText("普通用户")).toBeInTheDocument();
    expect(screen.getByText("200 GiB")).toBeInTheDocument(); // 配额换算显示。
    expect(screen.getByTestId("user-row-2").textContent).toContain("5.00 GiB");
    expect(screen.getByTestId("user-row-2").textContent).toContain("2.00 GiB");
    expect(screen.getByText("待删除")).toBeInTheDocument();
    expect(screen.getByText("初始密码")).toBeInTheDocument();
  });

  it("shows the empty state", async () => {
    setup(() => list([]));
    expect(await screen.findByText("暂无用户")).toBeInTheDocument();
  });

  it("shows the error state with retry", async () => {
    setup((_url, init) => {
      if (init?.method === "GET") {
        return json(500, { error: { code: "internal", message: "后端炸了" } });
      }
      return list([]);
    });
    expect(await screen.findByText("后端炸了")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重试" })).toBeInTheDocument();
  });

  it("redirects non-admin users and never lists users", async () => {
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
    expect(mockFetch.mock.calls.find(([url]) => url.includes("/api/v2/users"))).toBeUndefined();
  });

  it("paginates to page 2", async () => {
    setup((url) => {
      if (url.includes("page=2")) return list([user(2)], 21);
      return list([user(1)], 21);
    });
    await screen.findByTestId("user-row-1");
    fireEvent.click(screen.getByLabelText("下一页"));
    await screen.findByTestId("user-row-2");
    await waitFor(() => {
      const url = mockFetch.mock.calls[mockFetch.mock.calls.length - 1][0] as string;
      expect(url).toContain("page=2");
    });
  });
});

describe("UsersPage create", () => {
  it("creates a user with hashed password and quota bytes", async () => {
    setup((_url, init) => {
      if (init?.method === "POST" && _url === "/api/v2/users") {
        return user(9, { username: "bob" });
      }
      return list([ME]);
    });
    await screen.findByTestId("user-row-1");
    fireEvent.click(screen.getByRole("button", { name: "创建用户" }));
    await screen.findByRole("dialog", { name: "创建用户" });
    fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "bob" } });
    fireEvent.change(screen.getByLabelText("初始口令（至少 6 位）"), { target: { value: "secret123" } });
    fireEvent.change(screen.getByLabelText("配额（GiB）"), { target: { value: "50" } });
    fireEvent.click(screen.getByRole("button", { name: "创建" }));
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(
        ([url, init2]) => init2?.method === "POST" && url === "/api/v2/users",
      );
      expect(call).toBeDefined();
      const body = JSON.parse(call![1].body as string);
      expect(body.username).toBe("bob");
      // 独立基准（目标用户名 bob 派生）：pbkdf2_hmac('sha256', b'secret123', sha256(b'bob').hexdigest().encode(), 10000).hex()
      expect(body.password).toBe("ca95e432502136463589237a06357aa4c30db1f8be3e3a4a9cf6ac57df638337");
      expect(body.is_admin).toBe(false);
      expect(body.is_initial_password).toBe(true);
      expect(body.quota_bytes).toBe(50 * GIB);
    });
    expect(await screen.findByText("用户 bob 已创建")).toBeInTheDocument();
  });

  it("validates username, password and quota inline without submitting", async () => {
    setup(() => list([ME]));
    await screen.findByTestId("user-row-1");
    fireEvent.click(screen.getByRole("button", { name: "创建用户" }));
    await screen.findByRole("dialog", { name: "创建用户" });
    fireEvent.click(screen.getByRole("button", { name: "创建" }));
    expect(await screen.findByText("请输入用户名")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "bob" } });
    fireEvent.click(screen.getByRole("button", { name: "创建" }));
    expect(await screen.findByText("初始口令至少 6 位")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("初始口令（至少 6 位）"), { target: { value: "secret123" } });
    fireEvent.change(screen.getByLabelText("配额（GiB）"), { target: { value: "-2" } });
    fireEvent.click(screen.getByRole("button", { name: "创建" }));
    expect(await screen.findByText("配额必须是不小于 0 的数字")).toBeInTheDocument();
    expect(
      mockFetch.mock.calls.find(([, init2]) => init2?.method === "POST"),
    ).toBeUndefined();
  });

  it("shows duplicate-name conflict inline", async () => {
    setup((_url, init) => {
      if (init?.method === "POST" && _url === "/api/v2/users") {
        return json(409, { error: { code: "duplicate_resource", message: "用户名已存在" } });
      }
      return list([ME]);
    });
    await screen.findByTestId("user-row-1");
    fireEvent.click(screen.getByRole("button", { name: "创建用户" }));
    await screen.findByRole("dialog", { name: "创建用户" });
    fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "bob" } });
    fireEvent.change(screen.getByLabelText("初始口令（至少 6 位）"), { target: { value: "secret123" } });
    fireEvent.click(screen.getByRole("button", { name: "创建" }));
    expect(await screen.findByText("用户名已存在")).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "创建用户" })).toBeInTheDocument();
  });

  it("rejects usernames longer than 64 characters", async () => {
    setup(() => list([ME]));
    await screen.findByTestId("user-row-1");
    fireEvent.click(screen.getByRole("button", { name: "创建用户" }));
    await screen.findByRole("dialog", { name: "创建用户" });
    fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "x".repeat(65) } });
    fireEvent.change(screen.getByLabelText("初始口令（至少 6 位）"), { target: { value: "secret123" } });
    fireEvent.click(screen.getByRole("button", { name: "创建" }));
    expect(await screen.findByText("用户名最多 64 个字符")).toBeInTheDocument();
  });

  it("exercises every create-dialog control", async () => {
    setup((_url, init) => {
      if (init?.method === "POST" && _url === "/api/v2/users") return user(9);
      return list([ME]);
    });
    await screen.findByTestId("user-row-1");
    fireEvent.click(screen.getByRole("button", { name: "创建用户" }));
    await screen.findByRole("dialog", { name: "创建用户" });
    // 全部控件各触发一次 change（含两个开关），覆盖各内联处理器。
    fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "bob" } });
    fireEvent.change(screen.getByLabelText("初始口令（至少 6 位）"), { target: { value: "secret123" } });
    fireEvent.change(screen.getByLabelText("配额（GiB）"), { target: { value: "10" } });
    fireEvent.click(screen.getByLabelText("设为管理员"));
    fireEvent.click(screen.getByLabelText("标记为初始密码（用户首次修改免旧密码）"));
    fireEvent.click(screen.getByRole("button", { name: "创建" }));
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(
        ([url, init2]) => init2?.method === "POST" && url === "/api/v2/users",
      );
      expect(JSON.parse(call![1].body as string).is_admin).toBe(true);
    });
  });
});

describe("UsersPage edit", () => {
  it("sends only fields converted from GiB to bytes and reloads", async () => {
    setup((_url, init) => {
      if (init?.method === "PATCH") {
        return user(2, { username: "renamed", quota_bytes: 64 * GIB });
      }
      return list([ME, user(2, { quota_bytes: 32 * GIB })]);
    });
    await screen.findByTestId("user-row-2");
    fireEvent.click(withinRow(2, "编辑"));
    const dialog = await screen.findByRole("dialog", { name: "编辑用户：user-2" });
    expect(dialog.textContent).toContain("当前 32.0 GiB");
    fireEvent.change(screen.getByLabelText("配额（GiB，当前 32.0 GiB）"), { target: { value: "64" } });
    fireEvent.click(screen.getAllByRole("button", { name: "保存" }).at(-1)!);
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([, init2]) => init2?.method === "PATCH");
      expect(JSON.parse(call![1].body as string)).toEqual({
        username: "user-2",
        is_admin: false,
        is_initial_password: false,
        quota_bytes: 64 * GIB,
      });
    });
    expect(await screen.findByText("用户 user-2 已更新")).toBeInTheDocument();
  });

  it("keeps save disabled until something changes; cancel submits nothing", async () => {
    setup(() => list([ME, user(2)]));
    await screen.findByTestId("user-row-2");
    fireEvent.click(withinRow(2, "编辑"));
    await screen.findByRole("dialog", { name: "编辑用户：user-2" });
    const save = screen.getAllByRole("button", { name: "保存" }).at(-1)! as HTMLButtonElement;
    expect(save.disabled).toBe(true); // 无变更不可保存。
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(mockFetch.mock.calls.find(([, init2]) => init2?.method === "PATCH")).toBeUndefined();
  });

  it("disables role editing for the current admin self", async () => {
    setup(() => list([ME, user(2)]));
    await screen.findByTestId("user-row-1");
    fireEvent.click(withinRow(1, "编辑"));
    await screen.findByRole("dialog", { name: "编辑用户：admin" });
    const adminToggle = screen.getByLabelText("管理员（不能修改自己的角色）") as HTMLInputElement;
    expect(adminToggle.disabled).toBe(true);
  });

  it("validates edit username and quota inline", async () => {
    setup(() => list([ME, user(2)]));
    await screen.findByTestId("user-row-2");
    fireEvent.click(withinRow(2, "编辑"));
    await screen.findByRole("dialog", { name: "编辑用户：user-2" });
    fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "" } });
    fireEvent.click(screen.getAllByRole("button", { name: "保存" }).at(-1)!);
    expect(await screen.findByText("请输入用户名")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "user-2" } });
    fireEvent.change(screen.getByLabelText("配额（GiB，当前 100 GiB）"), { target: { value: "abc" } });
    fireEvent.click(screen.getAllByRole("button", { name: "保存" }).at(-1)!);
    expect(await screen.findByText("配额必须是不小于 0 的数字")).toBeInTheDocument();
    expect(mockFetch.mock.calls.find(([, init2]) => init2?.method === "PATCH")).toBeUndefined();
  });

  it("exercises remaining edit controls and PATCH failure path", async () => {
    setup((_url, init) => {
      if (init?.method === "PATCH") {
        return json(409, { error: { code: "duplicate_resource", message: "用户名已存在" } });
      }
      return list([ME, user(2)]);
    });
    await screen.findByTestId("user-row-2");
    fireEvent.click(withinRow(2, "编辑"));
    await screen.findByRole("dialog", { name: "编辑用户：user-2" });
    fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "renamed" } });
    fireEvent.click(screen.getByLabelText("管理员"));
    fireEvent.click(screen.getByLabelText("标记为初始密码"));
    fireEvent.click(screen.getAllByRole("button", { name: "保存" }).at(-1)!);
    await waitFor(() => {
      const call = mockFetch.mock.calls.find(([, init2]) => init2?.method === "PATCH");
      expect(JSON.parse(call![1].body as string)).toEqual({
        username: "renamed",
        is_admin: true,
        is_initial_password: true,
        quota_bytes: 100 * GIB,
      });
    });
    expect(await screen.findByText("用户名已存在")).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "编辑用户：user-2" })).toBeInTheDocument();
  });
});

describe("UsersPage delete", () => {
  it("blocks deleting self in the row", async () => {
    setup(() => list([ME, user(2)]));
    await screen.findByTestId("user-row-1");
    const selfDelete = screen.getByTestId("user-row-1").querySelector(
      ".actions-cell button:last-child",
    ) as HTMLButtonElement;
    expect(selfDelete.disabled).toBe(true);
  });

  it("confirms then accepts 202 pending with Chinese toast", async () => {
    setup((_url, init) => {
      if (init?.method === "DELETE") return { ok: true, state: "pending", accepted: true };
      return list([ME, user(2, { pending_delete: true })]);
    });
    await screen.findByTestId("user-row-2");
    fireEvent.click(withinRow(2, "删除"));
    const dialog = await screen.findByRole("dialog", { name: "删除用户" });
    expect(dialog.textContent).toContain("后台清理");
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(mockFetch.mock.calls.find(([, init2]) => init2?.method === "DELETE")).toBeUndefined();

    fireEvent.click(withinRow(2, "删除"));
    await screen.findByRole("dialog", { name: "删除用户" });
    fireEvent.click(screen.getAllByRole("button", { name: "删除" }).at(-1)!);
    expect(await screen.findByText("用户 user-2 删除已受理，后台清理进行中")).toBeInTheDocument();
  });

  it("surfaces delete failure via toast", async () => {
    setup((_url, init) => {
      if (init?.method === "DELETE") {
        return json(409, { error: { code: "content_busy", message: "该用户仍有进行中任务" } });
      }
      return list([ME, user(2)]);
    });
    await screen.findByTestId("user-row-2");
    fireEvent.click(withinRow(2, "删除"));
    await screen.findByRole("dialog", { name: "删除用户" });
    fireEvent.click(screen.getAllByRole("button", { name: "删除" }).at(-1)!);
    expect(await screen.findByText("该用户仍有进行中任务")).toBeInTheDocument();
  });
});

function withinRow(id: number, name: string): HTMLElement {
  const button = screen
    .getByTestId(`user-row-${id}`)
    .querySelector(".actions-cell");
  const buttons = Array.from(button?.querySelectorAll("button") ?? []);
  return buttons.find((element) => element.textContent === name) as HTMLElement;
}

describe("users api helpers", () => {
  const { gibToBytes, bytesToGib } = jest.requireActual("@/features/users/api") as {
    gibToBytes: (gib: number) => number | null;
    bytesToGib: (bytes: number) => number;
  };

  it("converts GiB to bytes and rejects invalid input", () => {
    expect(gibToBytes(1.5)).toBe(Math.round(1.5 * 1024 ** 3));
    expect(gibToBytes(0)).toBe(0);
    expect(gibToBytes(-1)).toBeNull();
    expect(gibToBytes(Number.NaN)).toBeNull();
    expect(gibToBytes(Number.POSITIVE_INFINITY)).toBeNull();
  });

  it("rounds bytes to two-decimal GiB", () => {
    expect(bytesToGib(1024 ** 3)).toBe(1);
    expect(bytesToGib(1.5 * 1024 ** 3)).toBe(1.5);
  });
});

describe("UsersPage additional branches", () => {
  it("rejects an empty quota field", async () => {
    setup(() => list([ME]));
    await screen.findByTestId("user-row-1");
    fireEvent.click(screen.getByRole("button", { name: "创建用户" }));
    await screen.findByRole("dialog", { name: "创建用户" });
    fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "bob" } });
    fireEvent.change(screen.getByLabelText("初始口令（至少 6 位）"), { target: { value: "secret123" } });
    fireEvent.change(screen.getByLabelText("配额（GiB）"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "创建" }));
    expect(await screen.findByText("请填写配额（GiB）")).toBeInTheDocument();
  });

  it("discards a stale list response after page change", async () => {
    let listCalls = 0;
    let releaseFirst: (value: unknown) => void = () => undefined;
    const firstList = new Promise((resolve) => {
      releaseFirst = resolve;
    });
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (url === "/api/v2/auth/me") return json(200, ME);
      if (init?.method === "POST" && url === "/api/v2/users") return json(200, user(9));
      listCalls += 1;
      if (listCalls === 1) return firstList as never; // 首次列表请求挂起。
      return json(200, list([user(1)]));
    });
    render(
      <MemoryRouter initialEntries={["/users"]}>
        <AppProviders>
          <UsersPage />
        </AppProviders>
      </MemoryRouter>,
    );
    await screen.findByRole("button", { name: "创建用户" });
    await waitFor(() => expect(listCalls).toBe(1));
    // 创建成功触发更新的列表请求（先返回），旧的首笔响应随后到达必须被丢弃。
    fireEvent.click(screen.getByRole("button", { name: "创建用户" }));
    await screen.findByRole("dialog", { name: "创建用户" });
    fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "bob" } });
    fireEvent.change(screen.getByLabelText("初始口令（至少 6 位）"), { target: { value: "secret123" } });
    fireEvent.click(screen.getByRole("button", { name: "创建" }));
    await screen.findByTestId("user-row-1");
    releaseFirst(json(200, list([user(42)])));
    await waitFor(() => {
      expect(screen.queryByTestId("user-row-42")).not.toBeInTheDocument();
    });
  });

  it("falls back a page when deleting the only row of the last page", async () => {
    setup((url, init) => {
      if (init?.method === "DELETE") {
        return { ok: true, state: "pending", accepted: true };
      }
      if (url.includes("page=2")) return list([user(2)], 21);
      return list([user(1)], 21);
    });
    await screen.findByTestId("user-row-1");
    fireEvent.click(screen.getByLabelText("下一页"));
    await screen.findByTestId("user-row-2");
    fireEvent.click(withinRow(2, "删除"));
    await screen.findByRole("dialog", { name: "删除用户" });
    fireEvent.click(screen.getAllByRole("button", { name: "删除" }).at(-1)!);
    await waitFor(() => {
      const url = mockFetch.mock.calls[mockFetch.mock.calls.length - 1][0] as string;
      expect(url).toContain("page=1");
    });
  });

  it("ignores a second create click while submitting", async () => {
    let release: (value: unknown) => void = () => undefined;
    const pending = new Promise((resolve) => {
      release = resolve;
    });
    setup((_url, init) => {
      if (init?.method === "POST" && _url === "/api/v2/users") return pending as never;
      return list([ME]);
    });
    await screen.findByTestId("user-row-1");
    fireEvent.click(screen.getByRole("button", { name: "创建用户" }));
    await screen.findByRole("dialog", { name: "创建用户" });
    fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "bob" } });
    fireEvent.change(screen.getByLabelText("初始口令（至少 6 位）"), { target: { value: "secret123" } });
    fireEvent.click(screen.getByRole("button", { name: "创建" }));
    await waitFor(() => {
      expect(
        mockFetch.mock.calls.filter(([, init2]) => init2?.method === "POST").length,
      ).toBe(1);
    });
    fireEvent.click(screen.getAllByRole("button", { name: "创建中…" })[0]); // 提交中重复点击。
    release(user(9));
    await waitFor(() => {
      expect(
        mockFetch.mock.calls.filter(([, init2]) => init2?.method === "POST").length,
      ).toBe(1); // 仍然只有一笔创建请求。
    });
  });
});

describe("UsersPage 编辑用户重置密码（U3）", () => {
  it("sends the target-username derived hash when resetting a password", async () => {
    setup((url, init) => {
      if (init?.method === "PATCH") return user(2);
      return list([user(2, { username: "bob" })]);
    });
    const row = await screen.findByTestId("user-row-2");
    fireEvent.click(within(row).getByRole("button", { name: "编辑" }));
    const dialog = await screen.findByRole("dialog", { name: /编辑用户：bob/ });
    // 留空时保存不可用（未修改任何内容）。
    expect(within(dialog).getByRole("button", { name: "保存" })).toBeDisabled();
    const input = within(dialog).getByLabelText("重置密码") as HTMLInputElement;
    expect(input.type).toBe("password");
    // 显示切换。
    fireEvent.click(within(dialog).getByRole("button", { name: "显示重置密码" }));
    expect(input.type).toBe("text");
    fireEvent.change(input, { target: { value: "new-secret" } });
    expect(within(dialog).getByRole("button", { name: "保存" })).toBeEnabled();
    fireEvent.click(within(dialog).getByRole("button", { name: "保存" }));
    await waitFor(() => {
      const patch = mockFetch.mock.calls.find(([, init2]) => init2?.method === "PATCH");
      expect(patch).toBeDefined();
      const body = JSON.parse(patch![1].body as string);
      // pbkdf2(sha256("bob"), "new-secret") 与登录侧同形态：64 位十六进制。
      expect(body.password).toMatch(/^[0-9a-f]{64}$/);
      expect(body.username).toBe("bob");
    });
    expect(await screen.findByText("用户 bob 已更新")).toBeInTheDocument();
  });

  it("omits password when the reset field is left blank and enforces minimum length", async () => {
    setup((url, init) => {
      if (init?.method === "PATCH") return user(2, { username: "bob" });
      return list([user(2, { username: "bob", quota_bytes: 2 * GIB })]);
    });
    const row = await screen.findByTestId("user-row-2");
    fireEvent.click(within(row).getByRole("button", { name: "编辑" }));
    const dialog = await screen.findByRole("dialog", { name: /编辑用户：bob/ });
    // 只改配额：不带 password 字段。
    const quota = within(dialog).getByLabelText("配额（GiB，当前 2.00 GiB）") as HTMLInputElement;
    fireEvent.change(quota, { target: { value: "3" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存" }));
    await waitFor(() => {
      const patch = mockFetch.mock.calls.find(([, init2]) => init2?.method === "PATCH");
      expect(patch).toBeDefined();
      expect(JSON.parse(patch![1].body as string)).toEqual({
        username: "bob",
        is_admin: false,
        is_initial_password: false,
        quota_bytes: 3 * GIB,
      });
    });

    // 过短密码内联报错且不发请求。
    const patchesBefore = mockFetch.mock.calls.filter(([, init2]) => init2?.method === "PATCH").length;
    fireEvent.click(within(row).getByRole("button", { name: "编辑" }));
    const reopened = await screen.findByRole("dialog", { name: /编辑用户：bob/ });
    fireEvent.change(within(reopened).getByLabelText("重置密码"), { target: { value: "123" } });
    fireEvent.click(within(reopened).getByRole("button", { name: "保存" }));
    expect(await screen.findByText("重置密码至少 6 位")).toBeInTheDocument();
    expect(
      mockFetch.mock.calls.filter(([, init2]) => init2?.method === "PATCH").length,
    ).toBe(patchesBefore);
  });
});
