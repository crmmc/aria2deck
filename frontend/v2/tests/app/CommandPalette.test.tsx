import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useEffect, useState } from "react";
import { MemoryRouter, Outlet, Route, Routes, useLocation } from "react-router-dom";
import { gsap } from "gsap";

import { CommandPalette } from "@/app/components/CommandPalette";
import { PageHeader } from "@/shared/components/PageHeader";
import { settingsCommands, taskCommands, type SettingsCommand, type TaskCommand } from "@/shared/lib/commands";

jest.mock("gsap", () => ({
  gsap: {
    killTweensOf: jest.fn(),
    set: jest.fn(),
    to: jest.fn(),
  },
}));

const setMock = jest.mocked(gsap.set);
const toMock = jest.mocked(gsap.to);
const originalMatchMedia = window.matchMedia;

const BASE_LABELS = [
  "添加下载任务",
  "批量添加任务",
  "上传种子文件",
  "打开文件管理",
  "打开任务历史",
  "打开分享管理",
  "打开用户设置",
];
const ADMIN_LABELS = ["打开基础设置", "打开实时连接设置", "打开安全与限流设置"];

function setReducedMotion(matches: boolean) {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    writable: true,
    value: jest.fn(() => ({ matches })),
  });
}

function lastToVars(): gsap.TweenVars {
  const call: unknown = toMock.mock.calls.at(-1);
  const vars: unknown = Array.isArray(call) ? call[1] : undefined;
  if (typeof vars !== "object" || vars === null) throw new Error("GSAP to 未收到动画参数");
  return vars as gsap.TweenVars;
}

function completeExit() {
  act(() => lastToVars().onComplete?.());
}

function TasksRoute() {
  const [command, setCommand] = useState<TaskCommand | null>(null);
  useEffect(() => taskCommands.subscribe(setCommand), []);
  return (
    <>
      <PageHeader title="任务" description="管理您的下载" />
      {command ? <div>任务动作:{command}</div> : null}
    </>
  );
}

function SettingsRoute() {
  const [command, setCommand] = useState<SettingsCommand | null>(null);
  const location = useLocation();
  useEffect(() => settingsCommands.subscribe(setCommand), []);
  return (
    <>
      <PageHeader title="系统设置" description="系统配置" />
      <div>设置路径:{location.pathname}</div>
      {command ? <div>设置动作:{command}</div> : null}
    </>
  );
}

function SimpleRoute({ title }: { title: string }) {
  return <PageHeader title={title} description={`${title}说明`} />;
}

function renderPalette(isAdmin = true, initialEntry = "/tasks") {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route path="/" element={<><CommandPalette isAdmin={isAdmin} /><Outlet /></>}>
          <Route path="tasks" element={<TasksRoute />} />
          <Route path="files" element={<SimpleRoute title="文件" />} />
          <Route path="history" element={<SimpleRoute title="任务历史" />} />
          <Route path="shares" element={<SimpleRoute title="分享管理" />} />
          <Route path="profile" element={<SimpleRoute title="用户设置" />} />
          <Route path="settings/:section?" element={<SettingsRoute />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

function openWithButton() {
  const trigger = screen.getByRole("button", { name: "打开快捷操作" });
  fireEvent.click(trigger);
  return { trigger, dialog: screen.getByRole("dialog", { name: "快捷操作" }) };
}

describe("CommandPalette", () => {
  let originalShowModal: HTMLDialogElement["showModal"] | undefined;
  let originalClose: HTMLDialogElement["close"] | undefined;

  beforeEach(() => {
    jest.clearAllMocks();
    setReducedMotion(false);
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
    if (originalMatchMedia) {
      Object.defineProperty(window, "matchMedia", {
        configurable: true,
        writable: true,
        value: originalMatchMedia,
      });
    } else {
      Reflect.deleteProperty(window, "matchMedia");
    }
  });

  it("uses the Demo block dialog and a full-width shell without a z-index override", () => {
    const css = readFileSync(resolve(__dirname, "../../src/styles/legacy/06-components.css"), "utf8");
    const dialogRule = css.match(/dialog\.command-dialog\s*\{([^}]*)\}/)?.[1];
    const shellRule = css.match(/(?:^|\n)\.command-shell\s*\{([^}]*)\}/)?.[1];
    expect(dialogRule).toMatch(/display:\s*block;/);
    expect(shellRule).toContain("width: 100%;");
    expect(shellRule).not.toMatch(/z-index\s*:/);
  });

  it("lists the Demo commands and groups for an administrator", () => {
    renderPalette(true);
    const { dialog } = openWithButton();
    expect(dialog.firstElementChild).toHaveClass("command-shell");
    expect(dialog.querySelector(".modal-backdrop-button")).toBeNull();
    expect(within(dialog).getAllByRole("button").map((button) => button.textContent)).toEqual([
      ...BASE_LABELS,
      ...ADMIN_LABELS,
    ]);
    expect(within(dialog).getByText("创建")).toBeInTheDocument();
    expect(within(dialog).getByText("页面导航")).toBeInTheDocument();
    expect(within(dialog).getByText("管理员")).toBeInTheDocument();
  });

  it("omits all administrator commands for a regular user", () => {
    renderPalette(false);
    const { dialog } = openWithButton();
    expect(within(dialog).getAllByRole("button").map((button) => button.textContent)).toEqual(BASE_LABELS);
    expect(within(dialog).queryByText("管理员")).not.toBeInTheDocument();
    ADMIN_LABELS.forEach((label) => {
      expect(within(dialog).queryByRole("button", { name: label })).not.toBeInTheDocument();
    });
  });

  it.each([
    { key: "k", ctrlKey: true, metaKey: false },
    { key: "K", ctrlKey: false, metaKey: true },
  ])("opens with $key keyboard modifier, selects search and enters from a hidden first frame", ({ key, ctrlKey, metaKey }) => {
    const selectSpy = jest.spyOn(HTMLInputElement.prototype, "select");
    try {
      renderPalette(true);
      fireEvent.keyDown(document, { key, ctrlKey, metaKey });
      const input = screen.getByLabelText("搜索操作");
      expect(input).toHaveFocus();
      expect(selectSpy).toHaveBeenCalled();
      expect(setMock).toHaveBeenCalledWith(document.querySelector(".command-shell"), {
        opacity: 0,
        scale: 0.86,
        x: 0,
        y: 0,
      });
      expect(lastToVars()).toMatchObject({ duration: 0.34, ease: "back.out(1.2)" });
    } finally {
      selectSpy.mockRestore();
    }
  });

  it("exits along the same trigger path in the real palette composition", async () => {
    const rectSpy = jest.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      const shell = this.classList.contains("command-shell");
      const rect = shell
        ? { left: 200, top: 100, width: 400, height: 300 }
        : { left: 250, top: 180, width: 100, height: 40 };
      return {
        ...rect,
        bottom: rect.top + rect.height,
        right: rect.left + rect.width,
        x: rect.left,
        y: rect.top,
        toJSON: () => rect,
      };
    });
    try {
      renderPalette(true);
      const { trigger, dialog } = openWithButton();
      expect(setMock).toHaveBeenCalledWith(document.querySelector(".command-shell"), {
        opacity: 0,
        scale: 0.25,
        x: -100,
        y: -50,
      });

      fireEvent(dialog, new Event("cancel", { bubbles: false, cancelable: true }));
      expect(lastToVars()).toMatchObject({
        opacity: 0,
        scale: 0.25,
        x: -100,
        y: -50,
        duration: 0.34,
        ease: "back.in(1.2)",
      });
      expect(screen.getByRole("dialog", { name: "快捷操作" })).toBeInTheDocument();

      completeExit();
      await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
      expect(trigger).toHaveFocus();
    } finally {
      rectSpy.mockRestore();
    }
  });

  it("filters labels and keywords and renders the empty state", () => {
    renderPalette(true);
    const { dialog } = openWithButton();
    const input = screen.getByLabelText("搜索操作");
    fireEvent.change(input, { target: { value: "websocket" } });
    expect(within(dialog).getAllByRole("button").map((button) => button.textContent)).toEqual([
      "打开实时连接设置",
    ]);
    fireEvent.change(input, { target: { value: "没有此操作" } });
    expect(within(dialog).queryByRole("button")).not.toBeInTheDocument();
    expect(within(dialog).getByText("没有匹配的操作")).toBeInTheDocument();
  });

  it("supports ArrowUp, ArrowDown and Enter", async () => {
    renderPalette(true);
    const { dialog } = openWithButton();
    const input = screen.getByLabelText("搜索操作");
    fireEvent.keyDown(input, { key: "ArrowUp" });
    expect(within(dialog).getByRole("button", { name: "打开安全与限流设置" })).toHaveClass("active");
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(within(dialog).getByRole("button", { name: "添加下载任务" })).toHaveClass("active");
    fireEvent.keyDown(input, { key: "ArrowDown" });
    expect(within(dialog).getByRole("button", { name: "批量添加任务" })).toHaveClass("active");
    fireEvent.keyDown(input, { key: "Enter" });
    expect(screen.queryByText("任务动作:batch")).not.toBeInTheDocument();
    completeExit();
    expect(await screen.findByText("任务动作:batch")).toBeInTheDocument();
  });

  it.each([
    ["添加下载任务", "add"],
    ["批量添加任务", "batch"],
    ["上传种子文件", "torrent"],
  ])("runs %s after navigating from another page", async (label, action) => {
    renderPalette(true, "/files");
    openWithButton();
    fireEvent.click(screen.getByRole("button", { name: label }));
    completeExit();
    expect(await screen.findByText(`任务动作:${action}`)).toBeInTheDocument();
  });

  it.each([
    ["打开文件管理", "文件"],
    ["打开任务历史", "任务历史"],
    ["打开分享管理", "分享管理"],
    ["打开用户设置", "用户设置"],
  ])("navigates with %s", async (label, title) => {
    renderPalette(true);
    openWithButton();
    fireEvent.click(screen.getByRole("button", { name: label }));
    expect(screen.getByRole("dialog", { name: "快捷操作" })).toBeInTheDocument();
    completeExit();
    expect(await screen.findByRole("heading", { name: title, level: 1 })).toBeInTheDocument();
  });

  it.each([
    ["打开基础设置", "basic"],
    ["打开实时连接设置", "realtime"],
    ["打开安全与限流设置", "security"],
  ])("dispatches %s to the real settings page", async (label, section) => {
    renderPalette(true, "/files");
    openWithButton();
    fireEvent.click(screen.getByRole("button", { name: label }));
    completeExit();
    expect(await screen.findByText(`设置动作:${section}`)).toBeInTheDocument();
    expect(screen.getByText(`设置路径:/settings/${section}`)).toBeInTheDocument();
  });

  it("closes with Escape after the exit and restores the trigger focus", async () => {
    renderPalette(true);
    const { trigger, dialog } = openWithButton();
    fireEvent(dialog, new Event("cancel", { bubbles: false, cancelable: true }));
    expect(screen.getByRole("dialog", { name: "快捷操作" })).toBeInTheDocument();
    completeExit();
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
  });

  it("keeps inside clicks open and closes on a real dialog click outside its bounds", async () => {
    renderPalette(true);
    const { trigger, dialog } = openWithButton();
    dialog.getBoundingClientRect = () => ({
      left: 100,
      top: 100,
      width: 600,
      height: 400,
      right: 700,
      bottom: 500,
      x: 100,
      y: 100,
      toJSON: () => ({}),
    });
    expect(dialog.querySelector(".modal-backdrop-button")).toBeNull();
    const entryTweenCount = toMock.mock.calls.length;
    fireEvent.click(dialog.querySelector(".command-shell")!, { clientX: 150, clientY: 150 });
    fireEvent.click(dialog, { clientX: 150, clientY: 150 });
    expect(toMock).toHaveBeenCalledTimes(entryTweenCount);
    expect(screen.getByRole("dialog", { name: "快捷操作" })).toBeInTheDocument();

    fireEvent.click(dialog, { clientX: 20, clientY: 20 });
    expect(toMock).toHaveBeenCalledTimes(entryTweenCount + 1);
    completeExit();
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
  });

  it("supports reduced motion without either GSAP tween", async () => {
    setReducedMotion(true);
    renderPalette(true);
    const { trigger, dialog } = openWithButton();
    expect(screen.getByLabelText("搜索操作")).toHaveFocus();
    expect(document.querySelector(".command-shell")).not.toHaveClass("overlay-animating");
    expect(toMock).not.toHaveBeenCalled();
    await waitFor(() => expect(dialog.style.opacity).toBe("1"));
    fireEvent(dialog, new Event("cancel", { bubbles: false, cancelable: true }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
    expect(dialog.style.opacity).toBe("");
    expect(toMock).not.toHaveBeenCalled();
  });
});
