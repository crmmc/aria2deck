import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { createTorrent } from "@/features/tasks/api";
import { TorrentCreateWizard } from "@/features/tasks/components/TorrentCreateWizard";
import type { TorrentPreviewResponse, TaskResponse } from "@/shared/api/types";

jest.mock("@/features/tasks/api", () => ({
  ...jest.requireActual("@/features/tasks/api"),
  createTorrent: jest.fn(),
}));

const createTorrentMock = createTorrent as jest.Mock;

let originalShowModal: HTMLDialogElement["showModal"] | undefined;
let originalClose: HTMLDialogElement["close"] | undefined;

beforeEach(() => {
  jest.clearAllMocks();
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

const TASK = { id: 9, name: "demo" } as unknown as TaskResponse;

const preview: TorrentPreviewResponse = {
  info_hash: "abc123",
  name: "demo",
  total_size: 3072,
  files: [
    { index: 1, path: "demo/子目录/a.bin", size: 2048 },
    { index: 2, path: "demo/子目录/b.txt", size: 1024 },
  ],
  default_selection: [1, 2],
};

function renderWizard() {
  return render(
    <TorrentCreateWizard
      torrentBase64="base64=="
      preview={preview}
      onCancel={jest.fn()}
      onCreated={jest.fn()}
      onError={jest.fn()}
    />,
  );
}

describe("TorrentCreateWizard", () => {
  it("按插入顺序构建文件树并默认全选", () => {
    renderWizard();
    expect(screen.getByText("已选文件").parentElement?.textContent).toContain("2 / 2");
    expect(screen.getByText("子目录")).toBeInTheDocument();
    expect(screen.getByText("a.bin")).toBeInTheDocument();
    expect(screen.getByText("b.txt")).toBeInTheDocument();
  });

  it("切换目录复选框联动子文件", () => {
    renderWizard();
    fireEvent.click(screen.getByRole("checkbox", { name: "子目录" }));
    expect(screen.getByText("已选文件").parentElement?.textContent).toContain("0 / 2");
    fireEvent.click(screen.getByRole("checkbox", { name: "子目录" }));
    expect(screen.getByText("已选文件").parentElement?.textContent).toContain("2 / 2");
  });

  it("单个文件复选框可独立切换选中", () => {
    renderWizard();
    expect(screen.getByText("已选文件").parentElement?.textContent).toContain("2 / 2");
    fireEvent.click(screen.getByRole("checkbox", { name: "a.bin" }));
    expect(screen.getByText("已选文件").parentElement?.textContent).toContain("1 / 2");
    fireEvent.click(screen.getByRole("checkbox", { name: "a.bin" }));
    expect(screen.getByText("已选文件").parentElement?.textContent).toContain("2 / 2");
  });

  it("「清空」取消全部选择、「全选」恢复全部", () => {
    renderWizard();
    fireEvent.click(screen.getByRole("button", { name: "清空" }));
    expect(screen.getByText("已选文件").parentElement?.textContent).toContain("0 / 2");
    fireEvent.click(screen.getByRole("button", { name: "全选" }));
    expect(screen.getByText("已选文件").parentElement?.textContent).toContain("2 / 2");
  });

  it("提交失败时向导内显示错误横幅且不关闭", async () => {
    createTorrentMock.mockRejectedValue(new Error("任务已存在"));
    const onError = jest.fn();
    render(
      <TorrentCreateWizard
        torrentBase64="base64=="
        preview={preview}
        onCancel={jest.fn()}
        onCreated={jest.fn()}
        onError={onError}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "下一阶段" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认" }));
    const banner = await screen.findByRole("alert");
    expect(banner).toHaveTextContent("任务已存在");
    expect(onError).toHaveBeenCalledWith("任务已存在");
    await waitFor(() => expect(screen.getByRole("button", { name: "确认" })).not.toBeDisabled());
  });

  it("提交成功回调 onCreated", async () => {
    createTorrentMock.mockResolvedValue(TASK);
    const onCreated = jest.fn();
    const onCancel = jest.fn();
    render(
      <TorrentCreateWizard
        torrentBase64="base64=="
        preview={preview}
        onCancel={onCancel}
        onCreated={onCreated}
        onError={jest.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "下一阶段" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认" }));
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(TASK));
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("取消需二次确认", async () => {
    const onCancel = jest.fn();
    render(
      <TorrentCreateWizard
        torrentBase64="base64=="
        preview={preview}
        onCancel={onCancel}
        onCreated={jest.fn()}
        onError={jest.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认取消" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("继续选择只关闭内层确认并把焦点还给外层取消按钮", () => {
    const onCancel = jest.fn();
    render(
      <TorrentCreateWizard
        torrentBase64="base64=="
        preview={preview}
        onCancel={onCancel}
        onCreated={jest.fn()}
        onError={jest.fn()}
      />,
    );
    const outerCancel = screen.getByRole("button", { name: "取消" });
    outerCancel.focus();
    fireEvent.click(outerCancel);
    const nested = screen.getByRole("dialog", { name: "取消添加任务" });
    const continueButton = within(nested).getByRole("button", { name: "继续选择" });
    expect(continueButton).toHaveFocus();

    fireEvent.click(continueButton);
    expect(screen.queryByRole("dialog", { name: "取消添加任务" })).not.toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "添加 BT 下载任务" })).toBeInTheDocument();
    expect(outerCancel).toHaveFocus();
    expect(onCancel).not.toHaveBeenCalled();
  });

  it("外层 Escape 只打开取消确认，不直接关闭向导", () => {
    const onCancel = jest.fn();
    render(
      <TorrentCreateWizard
        torrentBase64="base64=="
        preview={preview}
        onCancel={onCancel}
        onCreated={jest.fn()}
        onError={jest.fn()}
      />,
    );
    fireEvent(
      screen.getByRole("dialog", { name: "添加 BT 下载任务" }),
      new Event("cancel", { bubbles: false, cancelable: true }),
    );
    expect(screen.getByRole("dialog", { name: "取消添加任务" })).toBeInTheDocument();
    expect(onCancel).not.toHaveBeenCalled();
  });

  it("文件搜索过滤", () => {
    renderWizard();
    fireEvent.change(screen.getByLabelText("搜索文件"), { target: { value: "a.bin" } });
    expect(screen.getByText("a.bin")).toBeInTheDocument();
    expect(screen.queryByText("b.txt")).not.toBeInTheDocument();
  });
});
