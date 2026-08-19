import { readFileSync } from "fs";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import FilesPage from "@/app/(authenticated)/files/page";
import { ToastProvider } from "@/components/Toast";
import { api } from "@/lib/api";
import type { FileInfo, BrowseFileInfo, FileListResponse } from "@/types";
import type { ListProps } from "react-window";
import type { AutoSizerProps } from "react-virtualized-auto-sizer";

// Mock next/navigation
jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: jest.fn() }),
}));

// Mock react-virtualized-auto-sizer — render children with fixed dimensions
jest.mock("react-virtualized-auto-sizer", () => ({
  __esModule: true,
  AutoSizer: ({ renderProp }: AutoSizerProps) => {
    if (!renderProp) return null;
    return renderProp({ height: 600, width: 1200 });
  },
}));

// Mock react-window — render all rows without virtualization
jest.mock("react-window", () => ({
  List: ({ rowCount, rowComponent: Row, rowProps, style }: ListProps<Record<string, unknown>>) => (
    <div style={style} data-testid="virtual-list">
      {Array.from({ length: rowCount }, (_, i) => (
        <Row key={i} index={i} style={{}} {...rowProps} />
      ))}
    </div>
  ),
}));

// Mock api module
jest.mock("@/lib/api", () => ({
  __esModule: true,
  api: {
    listFiles: jest.fn(),
    browseFile: jest.fn(),
    searchFiles: jest.fn(),
    downloadFileUrl: jest.fn(),
    deleteFiles: jest.fn(),
    calculatePackSize: jest.fn(),
    getAvailableSpace: jest.fn(),
    createPackTask: jest.fn(),
    renameFile: jest.fn(),
  },
  authEvents: {
    listeners: new Set(),
    onUnauthorized: jest.fn().mockReturnValue(jest.fn()),
    emit: jest.fn(),
  },
  ApiError: class ApiError extends Error {},
}));

const mockApi = api as jest.Mocked<typeof api>;

// Test data
const folderFile: FileInfo = {
  id: 1,
  content_hash: "hash_folder",
  name: "MyFolder",
  size: 0,
  is_directory: true,
  created_at: "2024-01-01T00:00:00",
};

const regularFile: FileInfo = {
  id: 2,
  content_hash: "hash_readme",
  name: "readme.txt",
  size: 1024,
  is_directory: false,
  created_at: "2024-01-02T00:00:00",
};

const browseItems: BrowseFileInfo[] = [
  { name: "file1.txt", size: 100, is_directory: false },
  { name: "file2.txt", size: 200, is_directory: false },
  { name: "subdir", size: 0, is_directory: true },
];

const subfolderItems: BrowseFileInfo[] = [
  { name: "deep.txt", size: 50, is_directory: false },
];

function setupListFiles(files: FileInfo[] = [folderFile, regularFile]) {
  mockApi.listFiles.mockResolvedValue({
    files,
    total: files.length,
    space: { used: 1024, frozen: 0, available: 9216 },
  } satisfies FileListResponse);
}

function injectGlobalStyles() {
  const style = document.createElement("style");
  style.textContent = readFileSync(`${process.cwd()}/app/globals.css`, "utf8");
  document.head.appendChild(style);
  return () => style.remove();
}

/** Render page and wait for initial file list to load */
async function renderAndWait() {
  setupListFiles();
  render(
    <ToastProvider>
      <FilesPage />
    </ToastProvider>
  );
  await waitFor(() => {
    expect(screen.getByText("MyFolder")).toBeInTheDocument();
  });
}

/** Enter the folder from root */
async function enterFolder() {
  mockApi.browseFile.mockResolvedValue(browseItems);
  // Click the folder name button
  const folderBtn = screen.getByRole("button", { name: "MyFolder" });
  fireEvent.click(folderBtn);
  await waitFor(() => {
    expect(screen.getByText("file1.txt")).toBeInTheDocument();
  });
}

beforeEach(() => {
  jest.clearAllMocks();
  // Suppress console.error from React (e.g. act warnings)
  jest.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  (console.error as jest.Mock).mockRestore?.();
});

describe("Folder in-page browsing", () => {
  test("desktop sortable headers render as flat table header controls", async () => {
    const removeStyles = injectGlobalStyles();
    try {
      await renderAndWait();

      const nameHeader = screen.getByRole("button", { name: /名称/ });
      const sizeHeader = screen.getByRole("button", { name: /大小/ });

      expect(window.getComputedStyle(nameHeader).display).toBe("flex");
      expect(window.getComputedStyle(nameHeader).backgroundColor).toBe("rgba(0, 0, 0, 0)");
      expect(window.getComputedStyle(nameHeader).borderTopWidth).toBe("0px");
      expect(window.getComputedStyle(sizeHeader).justifyContent).toBe("flex-end");
    } finally {
      removeStyles();
    }
  });

  test("clicking a folder shows folder contents with breadcrumb", async () => {
    await renderAndWait();
    await enterFolder();

    // Breadcrumb should show "根目录" as a clickable button and "MyFolder"
    const breadcrumbButtons = screen.getAllByRole("button", { name: /根目录/ });
    expect(breadcrumbButtons.length).toBeGreaterThan(0);

    // Folder contents visible
    expect(screen.getByText("file1.txt")).toBeInTheDocument();
    expect(screen.getByText("file2.txt")).toBeInTheDocument();
    expect(screen.getByText("subdir")).toBeInTheDocument();

    // Root file "readme.txt" should NOT be visible
    expect(screen.queryByText("readme.txt")).not.toBeInTheDocument();
  });

  test("clicking a subfolder navigates deeper with updated breadcrumb", async () => {
    await renderAndWait();
    await enterFolder();

    // Navigate into subdir
    mockApi.browseFile.mockResolvedValue(subfolderItems);
    const openBtn = screen.getByRole("button", { name: "打开" });
    fireEvent.click(openBtn);

    await waitFor(() => {
      expect(screen.getByText("deep.txt")).toBeInTheDocument();
    });

    // Breadcrumb should contain "subdir"
    expect(screen.getByRole("button", { name: "subdir" })).toBeInTheDocument();
    // browseFile called with path "subdir"
    expect(mockApi.browseFile).toHaveBeenCalledWith(folderFile.content_hash, "subdir");
  });

  test("clicking breadcrumb navigates back to folder root", async () => {
    await renderAndWait();
    await enterFolder();

    // Navigate into subdir
    mockApi.browseFile.mockResolvedValue(subfolderItems);
    const subdirBtn = screen.getByRole("button", { name: "subdir" });
    fireEvent.click(subdirBtn);
    await waitFor(() => {
      expect(screen.getByText("deep.txt")).toBeInTheDocument();
    });

    // Click "MyFolder" in breadcrumb to go back to folder root
    mockApi.browseFile.mockResolvedValue(browseItems);
    const myFolderBreadcrumb = screen.getByRole("button", { name: "MyFolder" });
    fireEvent.click(myFolderBreadcrumb);

    await waitFor(() => {
      expect(screen.getByText("file1.txt")).toBeInTheDocument();
    });
    // browseFile called without path (root of folder)
    expect(mockApi.browseFile).toHaveBeenLastCalledWith(folderFile.content_hash, undefined);
  });

  test("clicking 根目录 returns to file list", async () => {
    await renderAndWait();
    await enterFolder();

    // Click "根目录" button
    const rootBtn = screen.getByRole("button", { name: /根目录/ });
    fireEvent.click(rootBtn);

    // Should see original files again
    await waitFor(() => {
      expect(screen.getByText("MyFolder")).toBeInTheDocument();
      expect(screen.getByText("readme.txt")).toBeInTheDocument();
    });

    // Folder contents should be gone
    expect(screen.queryByText("file1.txt")).not.toBeInTheDocument();
  });

  test("all items have checkboxes including directories", async () => {
    await renderAndWait();
    await enterFolder();

    // Get all checkboxes: toolbar global search toggle + header + 3 items (file1.txt, file2.txt, subdir)
    const checkboxes = screen.getAllByRole("checkbox");
    expect(checkboxes).toHaveLength(5);
  });

  test("selecting only files and clicking batch download triggers download", async () => {
    mockApi.downloadFileUrl.mockImplementation(
      (fileHash: string, path?: string) => `http://test/download/${fileHash}/${path}`
    );
    await renderAndWait();
    await enterFolder();

    // After sort: toolbar-global[0], header[1], subdir[2], file1.txt[3], file2.txt[4]
    const checkboxes = screen.getAllByRole("checkbox");
    fireEvent.click(checkboxes[3]); // file1.txt

    // "批量下载" button should appear
    const downloadBtn = await screen.findByRole("button", { name: "批量下载" });
    fireEvent.click(downloadBtn);

    await waitFor(() => {
      expect(mockApi.downloadFileUrl).toHaveBeenCalled();
    });
    expect(mockApi.downloadFileUrl).toHaveBeenCalledWith(folderFile.content_hash, expect.any(String));
  });

  test("select all selects all items including directories", async () => {
    await renderAndWait();
    await enterFolder();

    // Click "全选" button
    const selectAllBtn = screen.getByRole("button", { name: "全选" });
    fireEvent.click(selectAllBtn);

    // Should show "已选 3 项" (2 files + 1 directory)
    await waitFor(() => {
      expect(screen.getByText(/已选 3 项/)).toBeInTheDocument();
    });
  });

  test("batch download with folder selected shows warning toast", async () => {
    await renderAndWait();
    await enterFolder();

    // Select all (includes subdir)
    const selectAllBtn = screen.getByRole("button", { name: "全选" });
    fireEvent.click(selectAllBtn);

    await waitFor(() => {
      expect(screen.getByText(/已选 3 项/)).toBeInTheDocument();
    });

    // Click batch download
    const downloadBtn = screen.getByRole("button", { name: "批量下载" });
    fireEvent.click(downloadBtn);

    // Should show warning toast
    await waitFor(() => {
      expect(screen.getByText(/请仅选择文件/)).toBeInTheDocument();
    });
  });

  test("root batch download with folder selected shows warning toast", async () => {
    await renderAndWait();

    // Select all in root (includes MyFolder which is a directory)
    const selectAllBtn = screen.getByRole("button", { name: "全选" });
    fireEvent.click(selectAllBtn);

    await waitFor(() => {
      expect(screen.getByText(/已选 2 项/)).toBeInTheDocument();
    });

    // Click batch download
    const downloadBtn = screen.getByRole("button", { name: "批量下载" });
    fireEvent.click(downloadBtn);

    // Should show warning toast
    await waitFor(() => {
      expect(screen.getByText(/无法批量下载/)).toBeInTheDocument();
    });
  });

  test("root batch download with only files succeeds", async () => {
    mockApi.listFiles.mockResolvedValue({
      files: [regularFile],
      total: 1,
      space: { used: 1024, frozen: 0, available: 9216 },
    } satisfies FileListResponse);
    mockApi.downloadFileUrl.mockImplementation(
      (fileHash: string) => `http://test/download/${fileHash}`
    );

    render(
      <ToastProvider>
        <FilesPage />
      </ToastProvider>
    );
    await waitFor(() => {
      expect(screen.getByText("readme.txt")).toBeInTheDocument();
    });

    // Select the file
    const selectAllBtn = screen.getByRole("button", { name: "全选" });
    fireEvent.click(selectAllBtn);

    // Click batch download
    const downloadBtn = await screen.findByRole("button", { name: "批量下载" });
    fireEvent.click(downloadBtn);

    await waitFor(() => {
      expect(mockApi.downloadFileUrl).toHaveBeenCalledWith(regularFile.content_hash);
    });
  });

  test("root batch delete sends a single batch request", async () => {
    // jsdom 未实现 <dialog>.showModal，需模拟打开状态才能查询到对话框内内容
    const originalShowModal = HTMLDialogElement.prototype.showModal;
    const originalClose = HTMLDialogElement.prototype.close;
    Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
      configurable: true,
      value: function showModal(this: HTMLDialogElement) {
        this.setAttribute("open", "");
      },
    });
    Object.defineProperty(HTMLDialogElement.prototype, "close", {
      configurable: true,
      value: function close(this: HTMLDialogElement) {
        this.removeAttribute("open");
      },
    });

    mockApi.listFiles.mockResolvedValue({
      files: [regularFile],
      total: 1,
      space: { used: 1024, frozen: 0, available: 9216 },
    } satisfies FileListResponse);
    mockApi.deleteFiles.mockResolvedValue({
      accepted_count: 1,
      failed_count: 0,
      results: [
        {
          content_hash: regularFile.content_hash,
          ok: true,
          state: "pending",
          accepted: true,
          error: null,
        },
      ],
    });

    render(
      <ToastProvider>
        <FilesPage />
      </ToastProvider>
    );
    await waitFor(() => {
      expect(screen.getByText("readme.txt")).toBeInTheDocument();
    });

    // Select the file
    const selectAllBtn = screen.getByRole("button", { name: "全选" });
    fireEvent.click(selectAllBtn);

    // Click batch delete and confirm
    const deleteBtn = await screen.findByRole("button", { name: "批量删除" });
    fireEvent.click(deleteBtn);
    const confirmBtn = await within(
      await screen.findByRole("dialog")
    ).findByRole("button", { name: "删除" });
    fireEvent.click(confirmBtn);

    await waitFor(() => {
      expect(mockApi.deleteFiles).toHaveBeenCalledTimes(1);
    });
    expect(mockApi.deleteFiles).toHaveBeenCalledWith([
      regularFile.content_hash,
    ]);

    await waitFor(() => {
      expect(screen.getByText(/已删除 1 个文件/)).toBeInTheDocument();
    });

    Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
      configurable: true,
      value: originalShowModal,
    });
    Object.defineProperty(HTMLDialogElement.prototype, "close", {
      configurable: true,
      value: originalClose,
    });
  });

  test("inline delete sends a single-element array request", async () => {
    // jsdom 未实现 <dialog>.showModal，需模拟打开状态才能查询到对话框内内容
    const originalShowModal = HTMLDialogElement.prototype.showModal;
    const originalClose = HTMLDialogElement.prototype.close;
    Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
      configurable: true,
      value: function showModal(this: HTMLDialogElement) {
        this.setAttribute("open", "");
      },
    });
    Object.defineProperty(HTMLDialogElement.prototype, "close", {
      configurable: true,
      value: function close(this: HTMLDialogElement) {
        this.removeAttribute("open");
      },
    });

    mockApi.listFiles.mockResolvedValue({
      files: [regularFile],
      total: 1,
      space: { used: 1024, frozen: 0, available: 9216 },
    } satisfies FileListResponse);
    mockApi.deleteFiles.mockResolvedValue({
      accepted_count: 1,
      failed_count: 0,
      results: [
        {
          content_hash: regularFile.content_hash,
          ok: true,
          state: "pending",
          accepted: true,
          error: null,
        },
      ],
    });

    render(
      <ToastProvider>
        <FilesPage />
      </ToastProvider>
    );
    await waitFor(() => {
      expect(screen.getByText("readme.txt")).toBeInTheDocument();
    });

    // Click the row inline delete button and confirm
    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    const confirmBtn = await within(
      await screen.findByRole("dialog")
    ).findByRole("button", { name: "删除" });
    fireEvent.click(confirmBtn);

    await waitFor(() => {
      expect(mockApi.deleteFiles).toHaveBeenCalledTimes(1);
    });
    expect(mockApi.deleteFiles).toHaveBeenCalledWith([
      regularFile.content_hash,
    ]);

    Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
      configurable: true,
      value: originalShowModal,
    });
    Object.defineProperty(HTMLDialogElement.prototype, "close", {
      configurable: true,
      value: originalClose,
    });
  });

  test("inline delete failure shows error toast and skips reload", async () => {
    // jsdom 未实现 <dialog>.showModal，需模拟打开状态才能查询到对话框内内容
    const originalShowModal = HTMLDialogElement.prototype.showModal;
    const originalClose = HTMLDialogElement.prototype.close;
    Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
      configurable: true,
      value: function showModal(this: HTMLDialogElement) {
        this.setAttribute("open", "");
      },
    });
    Object.defineProperty(HTMLDialogElement.prototype, "close", {
      configurable: true,
      value: function close(this: HTMLDialogElement) {
        this.removeAttribute("open");
      },
    });

    mockApi.listFiles.mockResolvedValue({
      files: [regularFile],
      total: 1,
      space: { used: 1024, frozen: 0, available: 9216 },
    } satisfies FileListResponse);
    mockApi.deleteFiles.mockResolvedValue({
      accepted_count: 0,
      failed_count: 1,
      results: [
        {
          content_hash: regularFile.content_hash,
          ok: false,
          state: "failed",
          accepted: false,
          error: "文件不存在",
        },
      ],
    });

    render(
      <ToastProvider>
        <FilesPage />
      </ToastProvider>
    );
    await waitFor(() => {
      expect(screen.getByText("readme.txt")).toBeInTheDocument();
    });

    // Click the row inline delete button and confirm
    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    const confirmBtn = await within(
      await screen.findByRole("dialog")
    ).findByRole("button", { name: "删除" });
    fireEvent.click(confirmBtn);

    await waitFor(() => {
      expect(mockApi.deleteFiles).toHaveBeenCalledTimes(1);
    });

    await waitFor(() => {
      expect(screen.getByText(/删除失败：文件不存在/)).toBeInTheDocument();
    });
    // 条目失败时不刷新列表
    expect(mockApi.listFiles).toHaveBeenCalledTimes(1);

    Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
      configurable: true,
      value: originalShowModal,
    });
    Object.defineProperty(HTMLDialogElement.prototype, "close", {
      configurable: true,
      value: originalClose,
    });
  });

  test("pagination loads the next page only once", async () => {
    mockApi.listFiles.mockResolvedValue({
      files: [regularFile],
      total: 25,
      space: { used: 1024, frozen: 0, available: 9216 },
    } satisfies FileListResponse);

    render(
      <ToastProvider>
        <FilesPage />
      </ToastProvider>
    );
    await waitFor(() => {
      expect(screen.getByText("readme.txt")).toBeInTheDocument();
    });
    expect(mockApi.listFiles).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "下一页" }));

    await waitFor(() => {
      expect(mockApi.listFiles).toHaveBeenCalledTimes(2);
    });
    expect(mockApi.listFiles).toHaveBeenLastCalledWith(2, 10);
  });

  test("search input stays enabled inside folder", async () => {
    await renderAndWait();
    await enterFolder();

    // The search input group must not be disabled anymore
    const searchGroup = document.querySelector(".search-input-group");
    expect(searchGroup?.className).not.toContain("pointer-events-none");
    expect(screen.getByRole("textbox", { name: "搜索文件" })).toBeEnabled();
  });

  test("search results use native buttons", async () => {
    mockApi.searchFiles.mockResolvedValue({
      items: [
        {
          user_file_id: regularFile.id,
          content_hash: regularFile.content_hash,
          name: regularFile.name,
          size: regularFile.size,
          path: regularFile.name,
          is_directory: false,
          entry_path: null,
          rank: 0,
          root_index: 1,
        },
      ],
      total: 1,
      truncated: false,
    });
    await renderAndWait();

    const searchInput = screen.getByRole("textbox", { name: "搜索文件" });
    fireEvent.change(searchInput, { target: { value: "readme" } });
    fireEvent.keyDown(searchInput, { key: "Enter" });

    const locateButton = await screen.findByRole("button", { name: "定位" });
    expect(locateButton.tagName).toBe("BUTTON");
  });

  test("keyboard shortcut focuses the toolbar search input", async () => {
    await renderAndWait();

    fireEvent.keyDown(window, { key: "f", metaKey: true });

    expect(screen.getByRole("textbox", { name: "搜索文件" })).toHaveFocus();
  });

  test("sorting works inside folder — directories first", async () => {
    await renderAndWait();
    await enterFolder();

    // The virtual list should render items with directories first
    const list = screen.getByTestId("virtual-list");
    const textContent = list.textContent || "";

    // "subdir" (directory) should appear before "file1.txt" and "file2.txt"
    const subdirPos = textContent.indexOf("subdir");
    const file1Pos = textContent.indexOf("file1.txt");
    const file2Pos = textContent.indexOf("file2.txt");

    expect(subdirPos).toBeLessThan(file1Pos);
    expect(subdirPos).toBeLessThan(file2Pos);
  });
});
