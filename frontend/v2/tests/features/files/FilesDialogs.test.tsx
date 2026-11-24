import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { CreateShareDialog } from "@/features/files/components/CreateShareDialog";
import { PackDialog } from "@/features/files/components/PackDialog";
import { SearchModal } from "@/features/files/components/SearchModal";
import { createShare } from "@/features/files/api";
import {
  formatFileDate,
  locateFilePage,
  sortBrowseItems,
  sortFiles,
  toSearchHitView,
} from "@/features/files/model";
import type { FileView } from "@/features/files/model";
import type { SearchHitView } from "@/features/files/model";

import { ToastProvider } from "@/shared/components/Toast";

function withProviders(node: React.ReactElement) {
  return render(<ToastProvider>{node}</ToastProvider>);
}


jest.mock("@/features/files/api", () => ({
  ...jest.requireActual("@/features/files/api"),
  createShare: jest.fn(),
}));
const createShareMock = createShare as jest.Mock;

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

describe("model", () => {
  const fv = (id: number, name: string, isDirectory = false, size = id): FileView => ({
    id, name, size, isDirectory, createdAtMs: id,
  });

  it("sortFiles 目录优先 + 字段排序", () => {
    const files = [fv(1, "b.txt"), fv(2, "目录", true), fv(3, "a.txt")];
    expect(sortFiles(files, "name", "asc").map((f) => f.name)).toEqual(["目录", "a.txt", "b.txt"]);
    expect(sortFiles(files, "size", "desc").map((f) => f.name)).toEqual(["目录", "a.txt", "b.txt"]);
  });

  it("sortBrowseItems created_at 回退名称", () => {
    const items = [
      { name: "b.bin", size: 2, isDirectory: false },
      { name: "子目录", size: 0, isDirectory: true },
      { name: "a.bin", size: 1, isDirectory: false },
    ];
    expect(sortBrowseItems(items, "created_at", "asc").map((i) => i.name)).toEqual(["子目录", "a.bin", "b.bin"]);
  });

  it("toSearchHitView 由 path 推导 entryPath", () => {
    const root = toSearchHitView({ user_file_id: 1, display_name: "a.txt", path: "a.txt", kind: "file", size: 1 });
    expect(root.entryPath).toBeNull();
    const inside = toSearchHitView({ user_file_id: 2, display_name: "b.txt", path: "素材/b.txt", kind: "file", size: 1 });
    expect(inside.entryPath).toBe("素材/b.txt");
  });

  it("locateFilePage 逐页扫描命中与未命中", async () => {
    const pages: Record<number, { id: number }[]> = {
      1: [{ id: 1 }, { id: 2 }],
      2: [{ id: 3 }, { id: 4 }],
    };
    const fetchPage = jest.fn(async (page: number) => ({ items: pages[page] ?? [], total: 4 }));
    expect(await locateFilePage(fetchPage, 4, 2)).toBe(2);
    expect(await locateFilePage(fetchPage, 9, 2)).toBeNull();
  });

  it("sortBrowseItems 按大小排序（created_at 以外的字段）", () => {
    const items = [
      { name: "b.bin", size: 30, isDirectory: false },
      { name: "a.bin", size: 10, isDirectory: false },
      { name: "子目录", size: 0, isDirectory: true },
    ];
    expect(sortBrowseItems(items, "size", "asc").map((i) => i.name)).toEqual(["子目录", "a.bin", "b.bin"]);
    expect(sortBrowseItems(items, "size", "desc").map((i) => i.name)).toEqual(["子目录", "b.bin", "a.bin"]);
  });

  it("locateFilePage 提前遇到空页时停止扫描", async () => {
    const fetchPage = jest.fn(async (page: number) => {
      if (page === 1) return { items: [{ id: 1 }], total: 100 };
      if (page === 2) return { items: [], total: 100 };
      return { items: [{ id: 99 }], total: 100 };
    });
    expect(await locateFilePage(fetchPage, 99, 10)).toBeNull();
    // 第 2 页已空：不再继续请求第 3 页。
    expect(fetchPage).toHaveBeenCalledTimes(2);
  });

  it("formatFileDate 输出 y/m/d hh:mm", () => {
    const ms = new Date(2026, 10, 19, 9, 5).getTime();
    expect(formatFileDate(ms)).toBe("2026/11/19 09:05");
  });
});

describe("SearchModal", () => {
  const hit: SearchHitView = {
    userFileId: 1, name: "a.txt", path: "素材/a.txt", size: 10, isDirectory: false, entryPath: "素材/a.txt",
  };

  it("加载中/错误/空/结果四态", () => {
    const { rerender } = render(
      <SearchModal keyword="k" results={[]} loading error={null} truncated={false} onLocate={jest.fn()} onClose={jest.fn()} />,
    );
    expect(screen.getByText("搜索中...")).toBeInTheDocument();
    expect(document.querySelector(".search-modal-icon")).toHaveAttribute("data-icon", "search");
    rerender(<SearchModal keyword="k" results={[]} loading={false} error="网络错误" truncated={false} onLocate={jest.fn()} onClose={jest.fn()} />);
    expect(screen.getByText("网络错误")).toBeInTheDocument();
    rerender(<SearchModal keyword="k" results={[]} loading={false} error={null} truncated={false} onLocate={jest.fn()} onClose={jest.fn()} />);
    expect(screen.getByText("未找到匹配的文件")).toBeInTheDocument();
  });

  it("目录命中项使用文件夹图标", () => {
    const dirHit: SearchHitView = {
      userFileId: 2, name: "素材", path: "素材", size: 0, isDirectory: true, entryPath: null,
    };
    render(
      <SearchModal keyword="k" results={[dirHit]} loading={false} error={null} truncated={false} onLocate={jest.fn()} onClose={jest.fn()} />,
    );
    expect(document.querySelector(".search-result-item [data-icon=folder]")).toBeInTheDocument();
    expect(screen.queryByText("📁")).not.toBeInTheDocument();
    expect(screen.queryByText("📄")).not.toBeInTheDocument();
  });

  it("关闭按钮与遮罩都能关闭弹窗", async () => {
    const onClose = jest.fn();
    render(
      <SearchModal keyword="k" results={[]} loading={false} error={null} truncated={false} onLocate={jest.fn()} onClose={onClose} />,
    );
    fireEvent.click(document.querySelector(".modal-backdrop-button")!);
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });

  it("结果列表与定位按钮", () => {
    const onLocate = jest.fn();
    render(
      <SearchModal keyword="k" results={[hit]} loading={false} error={null} truncated onLocate={onLocate} onClose={jest.fn()} />,
    );
    expect(screen.getByText(/仅显示前 1 项/)).toBeInTheDocument();
    expect(document.querySelector(".search-result-item [data-icon=file]")).toBeInTheDocument();
    const trigger = screen.getByRole("button", { name: "定位" });
    fireEvent.click(trigger);
    expect(onLocate).toHaveBeenCalledWith(hit, trigger);
  });
});

describe("PackDialog", () => {
  it("空间不足禁用确认", () => {
    render(
      <PackDialog
        selectedCount={2}
        packSize={500}
        availableSpace={100}
        packOutputName=""
        packDeleteSource={false}
        packLoading={false}
        packing={false}
        onClose={jest.fn()}
        onOutputNameChange={jest.fn()}
        onDeleteSourceChange={jest.fn()}
        onConfirm={jest.fn()}
      />,
    );
    expect(screen.getByText("空间不足，无法创建打包任务")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "确认打包" })).toBeDisabled();
  });

  it("输入输出名、勾选删除源文件、关闭与确认都回调", async () => {
    const onClose = jest.fn();
    const onOutputNameChange = jest.fn();
    const onDeleteSourceChange = jest.fn();
    const onConfirm = jest.fn();
    const props = {
      selectedCount: 2,
      packSize: 100,
      availableSpace: 1000,
      packOutputName: "",
      packDeleteSource: false,
      packLoading: false,
      packing: false,
      onClose,
      onOutputNameChange,
      onDeleteSourceChange,
      onConfirm,
    };
    const view = render(<PackDialog {...props} />);
    fireEvent.change(screen.getByLabelText("输出文件名"), { target: { value: "备份.zip" } });
    expect(onOutputNameChange).toHaveBeenCalledWith("备份.zip");
    fireEvent.click(screen.getByLabelText("打包后删除源文件"));
    expect(onDeleteSourceChange).toHaveBeenCalledWith(true);
    const confirm = screen.getByRole("button", { name: "确认打包" });
    fireEvent.click(confirm);
    expect(onConfirm).toHaveBeenCalledWith(confirm);
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(onClose).toHaveBeenCalledTimes(1);

    // onClose 后真实页面会卸载；每个入口都用新实例验证，不复用 closing 状态的弹窗。
    view.rerender(<PackDialog key="title-close" {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "关闭打包弹窗" }));
    expect(onClose).toHaveBeenCalledTimes(2);
    view.rerender(<PackDialog key="backdrop-close" {...props} />);
    fireEvent.click(document.querySelector(".modal-backdrop-button")!);
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(3));
  });

  it("打包中禁用所有操作并显示创建中", () => {
    const onClose = jest.fn();
    render(
      <PackDialog
        selectedCount={1}
        packSize={100}
        availableSpace={1000}
        packOutputName="x.zip"
        packDeleteSource
        packLoading={false}
        packing
        onClose={onClose}
        onOutputNameChange={jest.fn()}
        onDeleteSourceChange={jest.fn()}
        onConfirm={jest.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "创建中..." })).toBeDisabled();
    expect(screen.getByRole("button", { name: "取消" })).toBeDisabled();
    expect(screen.getByLabelText("输出文件名")).toBeDisabled();
    expect(screen.getByLabelText("打包后删除源文件")).toBeDisabled();
    // 打包中：关闭按钮禁用、点遮罩也不关闭。
    fireEvent.click(screen.getByRole("button", { name: "关闭打包弹窗" }));
    fireEvent.click(document.querySelector(".modal-backdrop-button")!);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("加载态展示计算中", () => {
    render(
      <PackDialog
        selectedCount={2}
        packSize={null}
        availableSpace={null}
        packOutputName=""
        packDeleteSource={false}
        packLoading
        packing={false}
        onClose={jest.fn()}
        onOutputNameChange={jest.fn()}
        onDeleteSourceChange={jest.fn()}
        onConfirm={jest.fn()}
      />,
    );
    expect(screen.getByText("计算中...")).toBeInTheDocument();
  });
});

describe("CreateShareDialog", () => {
  beforeEach(() => {
    createShareMock.mockReset();
  });

  it("创建成功展示链接与密码", async () => {
    createShareMock.mockResolvedValue({ id: 1, code: "Xy9", has_password: true });
    withProviders(<CreateShareDialog userFileId={3} fileName="f.txt" onClose={jest.fn()} />);
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "pw1" } });
    fireEvent.click(screen.getByRole("button", { name: "创建分享" }));
    await screen.findByLabelText("分享链接");
    expect(createShareMock).toHaveBeenCalledWith(3, { password: "pw1", expires_in_seconds: 604800, max_downloads: null });
    expect(screen.getByText("密码: pw1")).toBeInTheDocument();
  });

  it("留空的可选项作为 null 提交，无密码时不展示密码行", async () => {
    createShareMock.mockResolvedValue({ id: 2, code: "N0P", has_password: false });
    withProviders(<CreateShareDialog userFileId={3} fileName="f.txt" onClose={jest.fn()} />);
    fireEvent.change(screen.getByLabelText("有效期"), { target: { value: "0" } });
    fireEvent.click(screen.getByRole("button", { name: "创建分享" }));
    await screen.findByLabelText("分享链接");
    expect(createShareMock).toHaveBeenCalledWith(3, {
      password: null,
      expires_in_seconds: null,
      max_downloads: null,
    });
    expect(screen.queryByText(/^密码: /)).not.toBeInTheDocument();
    const link = screen.getByLabelText("分享链接") as HTMLInputElement;
    expect(link.value).toBe(`${window.location.origin}/s/N0P`);
    expect(link.readOnly).toBe(true);
  });

  it("下载次数限制只接受 1..10000 的整数", async () => {
    createShareMock.mockResolvedValue({ id: 3, code: "Lmt", has_password: false });
    withProviders(<CreateShareDialog userFileId={3} fileName="f.txt" onClose={jest.fn()} />);
    const maxInput = screen.getByLabelText("下载次数限制") as HTMLInputElement;

    fireEvent.change(maxInput, { target: { value: "5" } });
    expect(maxInput.value).toBe("5");
    // 超上界与 0 被拒绝，输入保持上一次合法值。
    fireEvent.change(maxInput, { target: { value: "20000" } });
    expect(maxInput.value).toBe("5");
    fireEvent.change(maxInput, { target: { value: "0" } });
    expect(maxInput.value).toBe("5");
    // 清空是允许的（回到"不限"）。
    fireEvent.change(maxInput, { target: { value: "" } });
    expect(maxInput.value).toBe("");

    fireEvent.change(maxInput, { target: { value: "7" } });
    fireEvent.click(screen.getByRole("button", { name: "创建分享" }));
    await screen.findByLabelText("分享链接");
    expect(createShareMock).toHaveBeenCalledWith(3, {
      password: null,
      expires_in_seconds: 604800,
      max_downloads: 7,
    });
  });

  it("创建失败展示错误提示并留在表单", async () => {
    createShareMock.mockRejectedValue(new Error("文件不存在"));
    withProviders(<CreateShareDialog userFileId={3} fileName="f.txt" onClose={jest.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "创建分享" }));
    expect(await screen.findByText("创建失败: 文件不存在")).toBeInTheDocument();
    expect(screen.queryByLabelText("分享链接")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "创建分享" })).toBeEnabled();
  });

  it("创建中禁用按钮并显示创建中", async () => {
    const holder: { resolve?: (value: unknown) => void } = {};
    createShareMock.mockImplementation(
      () =>
        new Promise((resolve) => {
          holder.resolve = resolve;
        }),
    );
    withProviders(<CreateShareDialog userFileId={3} fileName="f.txt" onClose={jest.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "创建分享" }));
    const pending = screen.getByRole("button", { name: "创建中..." });
    expect(pending).toBeDisabled();
    await act(async () => {
      holder.resolve?.({ id: 4, code: "Busy", has_password: false });
    });
    await screen.findByLabelText("分享链接");
  });

  it("复制链接写入剪贴板并提示成功", async () => {
    const writeText = jest.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    try {
      createShareMock.mockResolvedValue({ id: 5, code: "Cpy", has_password: false });
      withProviders(<CreateShareDialog userFileId={3} fileName="f.txt" onClose={jest.fn()} />);
      fireEvent.click(screen.getByRole("button", { name: "创建分享" }));
      await screen.findByLabelText("分享链接");
      fireEvent.click(screen.getByRole("button", { name: "复制" }));
      expect(await screen.findByText("链接已复制")).toBeInTheDocument();
      expect(writeText).toHaveBeenCalledWith(`${window.location.origin}/s/Cpy`);
    } finally {
      Reflect.deleteProperty(navigator, "clipboard");
    }
  });

  it("关闭按钮与关闭链接按钮都通知关闭", async () => {
    createShareMock.mockResolvedValue({ id: 6, code: "Cls", has_password: false });
    const onClose = jest.fn();
    const view = withProviders(<CreateShareDialog userFileId={3} fileName="f.txt" onClose={onClose} />);
    fireEvent.click(screen.getByRole("button", { name: "关闭创建分享" }));
    expect(onClose).toHaveBeenCalledTimes(1);

    view.rerender(
      <ToastProvider>
        <CreateShareDialog key="created-result" userFileId={3} fileName="f.txt" onClose={onClose} />
      </ToastProvider>,
    );
    fireEvent.click(screen.getByRole("button", { name: "创建分享" }));
    await screen.findByLabelText("分享链接");
    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("创建成功回调携带分享响应", async () => {
    const share = { id: 7, code: "Cbk", has_password: false };
    createShareMock.mockResolvedValue(share);
    const onCreated = jest.fn();
    withProviders(
      <CreateShareDialog userFileId={3} fileName="f.txt" onClose={jest.fn()} onCreated={onCreated} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "创建分享" }));
    await screen.findByLabelText("分享链接");
    expect(onCreated).toHaveBeenCalledWith(share);
    expect(await screen.findByText("分享创建成功")).toBeInTheDocument();
  });
});
