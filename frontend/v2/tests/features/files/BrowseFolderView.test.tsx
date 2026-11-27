import { render, screen, fireEvent } from "@testing-library/react";

import { BrowseFolderView } from "@/features/files/components/BrowseFolderView";
import type { BrowseContext } from "@/features/files/components/FileToolbar";
import type { BrowseItemView } from "@/features/files/model";

const CONTEXT: BrowseContext = { fileId: 7, fileName: "素材", path: [] };
const NESTED: BrowseContext = { fileId: 7, fileName: "素材", path: ["2026"] };

const ITEMS: BrowseItemView[] = [
  { name: "a.png", size: 2048, isDirectory: false },
  { name: "子目录", size: 0, isDirectory: true },
];

function baseProps() {
  return {
    isMobile: false,
    browseLoading: false,
    browseContext: CONTEXT,
    browseContents: ITEMS,
    sortedBrowseContents: ITEMS,
    selectedBrowseFiles: new Set<string>(),
    highlightName: null as string | null,
    onSort: jest.fn(),
    getSortIcon: jest.fn(() => "none" as const),
    onToggleAllBrowseFiles: jest.fn(),
    onToggleBrowseFileSelection: jest.fn(),
    onSelectionKeyDown: jest.fn(),
    onNavigateIntoSubfolder: jest.fn(),
    onDownload: jest.fn(),
    onUnavailableDelete: jest.fn(),
  };
}

let originalRaf: typeof window.requestAnimationFrame;
let rafCallbacks: FrameRequestCallback[];

beforeEach(() => {
  rafCallbacks = [];
  originalRaf = window.requestAnimationFrame;
  window.requestAnimationFrame = jest.fn((callback: FrameRequestCallback) => {
    rafCallbacks.push(callback);
    return rafCallbacks.length;
  });
  if (typeof Element.prototype.scrollIntoView !== "function") {
    Element.prototype.scrollIntoView = jest.fn();
  }
});

afterEach(() => {
  window.requestAnimationFrame = originalRaf;
});

describe("BrowseFolderView 状态", () => {
  it("加载中显示占位，不渲染表格", () => {
    render(<BrowseFolderView {...baseProps()} browseLoading />);
    expect(screen.getByText("加载中...")).toBeInTheDocument();
    expect(screen.queryByTestId("file-list")).not.toBeInTheDocument();
  });

  it("空目录显示空状态", () => {
    render(<BrowseFolderView {...baseProps()} browseContents={[]} sortedBrowseContents={[]} />);
    expect(screen.getByText("文件夹为空")).toBeInTheDocument();
    expect(screen.getByText("此文件夹中没有文件")).toBeInTheDocument();
  });
});

describe("BrowseFolderView 桌面表格", () => {
  it("渲染表头并回调排序与全选", () => {
    const props = baseProps();
    render(<BrowseFolderView {...props} />);
    expect(screen.getByTestId("file-list")).toBeInTheDocument();
    expect(screen.getByText("a.png")).toBeInTheDocument();
    expect(screen.getByText("-")).toBeInTheDocument();
    expect(screen.getByText("2.0 KB")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /名称/ }));
    expect(props.onSort).toHaveBeenCalledWith("name");
    fireEvent.click(screen.getByRole("button", { name: /大小/ }));
    expect(props.onSort).toHaveBeenCalledWith("size");

    fireEvent.click(screen.getByLabelText("全选"));
    expect(props.onToggleAllBrowseFiles).toHaveBeenCalled();
  });

  it("全选框按路径拼 key 判断是否全选", () => {
    const props = baseProps();
    const { rerender } = render(<BrowseFolderView {...props} />);
    const selectAll = screen.getByLabelText("全选") as HTMLInputElement;
    expect(selectAll.checked).toBe(false);

    rerender(
      <BrowseFolderView {...props} selectedBrowseFiles={new Set(["a.png", "子目录"])} />,
    );
    expect((screen.getByLabelText("全选") as HTMLInputElement).checked).toBe(true);
  });

  it("嵌套目录下选择 key 带上层路径", () => {
    const props = baseProps();
    render(
      <BrowseFolderView
        {...props}
        browseContext={NESTED}
        selectedBrowseFiles={new Set(["2026/a.png", "2026/子目录"])}
      />,
    );
    expect((screen.getByLabelText("全选") as HTMLInputElement).checked).toBe(true);
    const boxes = screen.getAllByLabelText("选择文件") as HTMLInputElement[];
    expect(boxes[0].checked).toBe(true);
  });

  it("选择框与目录/文件操作分别回调", () => {
    const props = baseProps();
    render(<BrowseFolderView {...props} />);
    fireEvent.click((screen.getAllByLabelText("选择文件") as HTMLInputElement[])[0]);
    expect(props.onToggleBrowseFileSelection).toHaveBeenCalledWith("a.png", false);

    // 目录名与「打开」都进入子目录。
    fireEvent.click(screen.getByRole("button", { name: "子目录" }));
    expect(props.onNavigateIntoSubfolder).toHaveBeenCalledWith("子目录");
    fireEvent.click(screen.getByRole("button", { name: "打开" }));
    expect(props.onNavigateIntoSubfolder).toHaveBeenCalledTimes(2);

    // 下载回调携带拼好的相对路径。
    fireEvent.click(screen.getByRole("button", { name: "下载" }));
    expect(props.onDownload).toHaveBeenCalledWith("a.png");
  });

  it("嵌套目录下下载路径包含上层目录", () => {
    const props = baseProps();
    render(<BrowseFolderView {...props} browseContext={NESTED} />);
    fireEvent.click(screen.getByRole("button", { name: "下载" }));
    expect(props.onDownload).toHaveBeenCalledWith("2026/a.png");
  });

  it("高亮项带 aria-current 并在渲染后滚动到可视区", () => {
    const scrollSpy = jest.fn();
    Element.prototype.scrollIntoView = scrollSpy;
    render(<BrowseFolderView {...baseProps()} highlightName="子目录" />);
    const highlighted = document.querySelector('[aria-current="true"]');
    expect(highlighted?.className).toContain("file-locate-highlight");
    expect(screen.getByText("子目录").closest('[aria-current="true"]')).toBeTruthy();
    expect(document.querySelectorAll('[aria-current="true"]')).toHaveLength(1);

    // 触发 rAF 回调后对高亮行调用 scrollIntoView。
    rafCallbacks.forEach((cb) => cb(0));
    expect(scrollSpy).toHaveBeenCalledWith({ block: "center" });
  });

  it("未高亮时不注册滚动回调", () => {
    render(<BrowseFolderView {...baseProps()} />);
    expect(rafCallbacks).toHaveLength(0);
  });
});

describe("BrowseFolderView 多选交互", () => {
  it("行复选框带 data-select-index，回调带完整相对路径与 shiftKey", () => {
    const props = baseProps();
    render(<BrowseFolderView {...props} browseContext={NESTED} />);
    const boxes = screen.getAllByLabelText("选择文件") as HTMLInputElement[];
    expect(boxes.map((box) => box.dataset.selectIndex)).toEqual(["0", "1"]);

    fireEvent.click(boxes[1], { shiftKey: true });
    expect(props.onToggleBrowseFileSelection).toHaveBeenCalledWith("2026/子目录", true);
  });

  it("桌面对移动端容器都把键盘事件交给 onSelectionKeyDown", () => {
    const props = baseProps();
    const { unmount } = render(<BrowseFolderView {...props} />);
    fireEvent.keyDown(screen.getByTestId("file-list"), { key: "Escape" });
    expect(props.onSelectionKeyDown).toHaveBeenCalledTimes(1);

    unmount();
    render(<BrowseFolderView {...props} isMobile />);
    fireEvent.keyDown(screen.getByTestId("file-list"), { key: "ArrowDown", shiftKey: true });
    expect(props.onSelectionKeyDown).toHaveBeenCalledTimes(2);
  });

  it("移动卡片选择回调带相对路径", () => {
    const props = baseProps();
    render(<BrowseFolderView {...props} isMobile selectedBrowseFiles={new Set(["子目录"])} />);
    const boxes = screen.getAllByLabelText(/^选择 /) as HTMLInputElement[];

    fireEvent.click(boxes[0], { shiftKey: true });
    expect(props.onToggleBrowseFileSelection).toHaveBeenCalledWith("a.png", true);
  });
});

describe("BrowseFolderView 移动卡片", () => {
  it("渲染卡片并用卡片尺寸显示目录占位符", () => {
    const props = baseProps();
    render(<BrowseFolderView {...props} isMobile />);
    // 移动端不渲染表头。
    expect(screen.queryByLabelText("全选")).not.toBeInTheDocument();
    const cards = document.querySelectorAll(".mobile-file-card");
    expect(cards).toHaveLength(2);
    expect(screen.getByText("2.0 KB")).toBeInTheDocument();
    expect(screen.getAllByText("-")).toHaveLength(1);

    fireEvent.click(screen.getAllByRole("button", { name: "下载" })[0]);
    expect(props.onDownload).toHaveBeenCalledWith("a.png");
    fireEvent.click(screen.getAllByRole("button", { name: "打开" })[0]);
    expect(props.onNavigateIntoSubfolder).toHaveBeenCalledWith("子目录");
  });

  it("移动端目录名可进入子目录", () => {
    const props = baseProps();
    render(<BrowseFolderView {...props} isMobile />);
    fireEvent.click(screen.getByRole("button", { name: "子目录" }));
    expect(props.onNavigateIntoSubfolder).toHaveBeenCalledWith("子目录");
  });

  it("移动端选择框回调条目，删除按钮提示不可用", () => {
    const props = baseProps();
    render(<BrowseFolderView {...props} isMobile selectedBrowseFiles={new Set(["子目录"])} />);
    const boxes = screen.getAllByLabelText(/^选择 /) as HTMLInputElement[];
    expect(boxes[0].checked).toBe(false);
    expect(boxes[1].checked).toBe(true);
    fireEvent.click(boxes[0]);
    expect(props.onToggleBrowseFileSelection).toHaveBeenCalledWith("a.png", false);

    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    expect(props.onUnavailableDelete).toHaveBeenCalled();
  });

  it("移动端高亮卡片带 aria-current", () => {
    render(<BrowseFolderView {...baseProps()} isMobile highlightName="a.png" />);
    const cards = document.querySelectorAll(".mobile-file-card");
    expect(cards[0].getAttribute("aria-current")).toBe("true");
    expect(cards[1].getAttribute("aria-current")).toBeNull();
  });

  it("移动端无高亮时不渲染 aria-current", () => {
    render(<BrowseFolderView {...baseProps()} isMobile />);
    expect(document.querySelector('[aria-current="true"]')).toBeNull();
  });
});
