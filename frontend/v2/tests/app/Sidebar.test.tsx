import React, { useState } from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { Sidebar } from "@/app/components/Sidebar";
import { DEFAULT_SITE_TITLE, setSiteTitle } from "@/shared/lib/siteTitle";
import type { UserResponse } from "@/shared/api/types";

const mockFetch = jest.fn();
global.fetch = mockFetch as unknown as typeof fetch;

const ADMIN: UserResponse = {
  id: 1, username: "admin", is_admin: true, quota_bytes: 1,
  is_initial_password: false, pending_delete: false,
  used_bytes: 0, reserved_bytes: 0, available_bytes: 1,
};

const REGULAR: UserResponse = { ...ADMIN, id: 2, username: "bob", is_admin: false };

function json(status: number, body: unknown): unknown {
  return { status, ok: status >= 200 && status < 300, json: () => Promise.resolve(body) };
}

let originalShowModal: HTMLDialogElement["showModal"] | undefined;
let originalClose: HTMLDialogElement["close"] | undefined;

beforeEach(() => {
  mockFetch.mockReset();
  setSiteTitle(DEFAULT_SITE_TITLE);
  mockFetch.mockImplementation(async (url: string) => {
    if (url === "/api/v2/auth/me") return json(200, ADMIN);
    if (url === "/api/v2/public/site-info") return json(200, { site_title: "团队下载站" });
    return json(200, {});
  });
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
  document.querySelectorAll("dialog").forEach((d) => d.removeAttribute("open"));
  if (originalShowModal) {
    Object.defineProperty(HTMLDialogElement.prototype, "showModal", { configurable: true, value: originalShowModal });
  } else {
    Reflect.deleteProperty(HTMLDialogElement.prototype, "showModal");
  }
  if (originalClose) {
    Object.defineProperty(HTMLDialogElement.prototype, "close", { configurable: true, value: originalClose });
  } else {
    Reflect.deleteProperty(HTMLDialogElement.prototype, "close");
  }
});

type SidebarHarnessProps = {
  user: UserResponse | null;
  initialPath?: string;
  expanded?: boolean;
};

function SidebarHarness({ user, initialPath = "/tasks", expanded = false }: SidebarHarnessProps) {
  const [isExpanded, setIsExpanded] = useState(expanded);
  return (
    <MemoryRouter initialEntries={[initialPath]}>
      <AppProviders>
        <Sidebar user={user} sidebarExpanded={isExpanded} setSidebarExpanded={setIsExpanded} />
        <Routes>
          <Route path="*" element={<div data-testid="current-route">route</div>} />
        </Routes>
      </AppProviders>
    </MemoryRouter>
  );
}

async function renderSidebar(props: SidebarHarnessProps) {
  const view = render(<SidebarHarness {...props} />);
  // AppProviders 会先拉 me / site-info，等它们落地后再断言。
  await waitFor(() => expect(document.querySelector(".sidebar")).toBeTruthy());
  return view;
}

describe("Sidebar（V1 侧栏与移动底栏）", () => {
  it("桌面侧栏折叠时只显示图标，鼠标进入展开并显示标题与文字", async () => {
    await renderSidebar({ user: ADMIN });
    const sidebar = document.querySelector(".sidebar");
    expect(sidebar?.className).not.toContain("expanded");
    expect(document.querySelector(".sidebar-title")).toBeNull();
    expect(document.querySelector(".nav-text")).toBeNull();

    fireEvent.mouseEnter(sidebar!);
    await waitFor(() => expect(document.querySelector(".sidebar-title")?.textContent).toBe("团队下载站"));
    expect(document.querySelector(".nav-text")?.textContent).toBe("任务");
    expect(sidebar?.className).toContain("expanded");

    fireEvent.mouseLeave(sidebar!);
    await waitFor(() => expect(document.querySelector(".sidebar-title")).toBeNull());
  });

  it("当前路由命中时对应导航项标记 active，包含子路径", async () => {
    await renderSidebar({ user: ADMIN, initialPath: "/files/sub/dir" });
    const active = document.querySelector(".nav-item.active");
    expect(active?.getAttribute("href")).toBe("/files");
  });

  it("根路径不匹配任何导航项（pathname 为空的分支由路由保证）", async () => {
    await renderSidebar({ user: ADMIN, initialPath: "/" });
    expect(document.querySelector(".nav-item.active")).toBeNull();
  });

  it("管理员看到全部导航项与分隔线，普通用户看不到管理员项", async () => {
    const { unmount } = await renderSidebar({ user: ADMIN });
    fireEvent.mouseEnter(document.querySelector(".sidebar")!);
    const adminNav = document.querySelector(".sidebar-nav");
    expect(adminNav?.textContent).toContain("多用户管理");
    expect(document.querySelector(".nav-divider")).toBeTruthy();
    unmount();

    await renderSidebar({ user: REGULAR });
    fireEvent.mouseEnter(document.querySelector(".sidebar")!);
    const userNav = document.querySelector(".sidebar-nav");
    expect(userNav?.textContent).toContain("任务历史");
    expect(userNav?.textContent).not.toContain("多用户管理");
    expect(userNav?.textContent).not.toContain("存储管理");
    expect(userNav?.textContent).not.toContain("系统设置");
    expect(document.querySelector(".nav-divider")).toBeNull();
  });

  it("用户为空时按普通用户渲染", async () => {
    await renderSidebar({ user: null });
    fireEvent.mouseEnter(document.querySelector(".sidebar")!);
    expect(document.querySelector(".sidebar-nav")?.textContent).not.toContain("多用户管理");
  });

  it("移动底栏包含四个入口与「更多」", async () => {
    await renderSidebar({ user: ADMIN });
    const bottom = document.querySelector(".bottom-nav");
    expect(bottom?.textContent).toContain("任务");
    expect(bottom?.textContent).toContain("历史");
    expect(bottom?.textContent).toContain("文件");
    expect(bottom?.textContent).toContain("分享");
    expect(bottom?.textContent).toContain("更多");
  });

  it("移动底栏按当前路径高亮", async () => {
    await renderSidebar({ user: ADMIN, initialPath: "/history" });
    const activeItem = document.querySelector(".bottom-nav-item.active");
    expect(activeItem?.getAttribute("href")).toBe("/history");
  });

  it("更多菜单：管理员可见管理项，点击后关闭并跳转", async () => {
    await renderSidebar({ user: ADMIN });
    fireEvent.click(screen.getByRole("button", { name: "更多" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("个人设置");
    expect(dialog).toHaveTextContent("多用户管理");
    expect(dialog).toHaveTextContent("存储管理");
    expect(dialog).toHaveTextContent("系统设置");
    expect(dialog).toHaveTextContent("退出登录");

    fireEvent.click(within(dialog).getByText("系统设置"));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it.each(["个人设置", "多用户管理", "存储管理", "系统设置"])(
    "更多菜单点击「%s」后收起菜单",
    async (label) => {
      await renderSidebar({ user: ADMIN });
      fireEvent.click(screen.getByRole("button", { name: "更多" }));
      const dialog = await screen.findByRole("dialog");
      fireEvent.click(within(dialog).getByText(label));
      await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    },
  );

  it("更多菜单：普通用户只有个人设置与退出登录", async () => {
    await renderSidebar({ user: REGULAR });
    fireEvent.click(screen.getByRole("button", { name: "更多" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("个人设置");
    expect(dialog).not.toHaveTextContent("多用户管理");
    expect(dialog).not.toHaveTextContent("存储管理");
    expect(dialog).not.toHaveTextContent("系统设置");
    expect(dialog).toHaveTextContent("退出登录");
  });

  it("更多菜单：关闭按钮与标题栏关闭按钮都能收起", async () => {
    await renderSidebar({ user: ADMIN });
    fireEvent.click(screen.getByRole("button", { name: "更多" }));
    await screen.findByRole("dialog");
    fireEvent.click(screen.getByRole("button", { name: "关闭更多菜单" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    fireEvent.click(screen.getByRole("button", { name: "更多" }));
    await screen.findByRole("dialog");
    // 点击遮罩关闭。
    fireEvent.click(document.querySelector(".modal-backdrop-button")!);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("更多菜单里的退出登录会调用登出接口并收起菜单", async () => {
    await renderSidebar({ user: ADMIN });
    fireEvent.click(screen.getByRole("button", { name: "更多" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByText("退出登录"));
    await waitFor(() => {
      const calls = mockFetch.mock.calls.filter(([url]) => url === "/api/v2/auth/logout");
      expect(calls).toHaveLength(1);
    });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("桌面侧栏底部退出登录调用登出接口", async () => {
    await renderSidebar({ user: ADMIN });
    fireEvent.click(document.querySelector(".logout-btn")!);
    await waitFor(() => {
      const calls = mockFetch.mock.calls.filter(([url]) => url === "/api/v2/auth/logout");
      expect(calls).toHaveLength(1);
    });
  });
});
