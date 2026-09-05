import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import FilesPage from "@/app/(authenticated)/files/page";
import { ToastProvider } from "@/components/Toast";
import { api } from "@/lib/api";
import type { FileInfo, BrowseFileInfo, FileListResponse, FileSearchItem, FileSearchResponse } from "@/types";

jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: jest.fn() }),
}));

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

interface ShareDialogStubProps {
  userFileId: number;
  fileName: string;
  onClose: () => void;
}

jest.mock("@/components/CreateShareDialog", () => ({
  __esModule: true,
  default: ({ fileName, onClose }: ShareDialogStubProps) => (
    <div>
      share-dialog:{fileName}
      <button type="button" onClick={onClose}>
        关闭分享
      </button>
    </div>
  ),
}));

interface PackTaskCardStubProps {
  onTaskComplete: () => void;
}

jest.mock("@/components/PackTaskCard", () => ({
  __esModule: true,
  default: ({ onTaskComplete }: PackTaskCardStubProps) => (
    <button type="button" onClick={onTaskComplete}>
      触发打包完成
    </button>
  ),
}));

const mockApi = api as jest.Mocked<typeof api>;

const folderFile: FileInfo = {
  id: 1,
  content_hash: "hash_folder",
  name: "MyFolder",
  size: 0,
  is_directory: true,
  created_at: "2024-01-01T00:00:00",
};

const alphaFile: FileInfo = {
  id: 2,
  content_hash: "hash_alpha",
  name: "alpha.txt",
  size: 100,
  is_directory: false,
  created_at: "2024-01-05T00:00:00",
};

const betaFile: FileInfo = {
  id: 3,
  content_hash: "hash_beta",
  name: "beta.txt",
  size: 50,
  is_directory: false,
  created_at: "2024-01-06T00:00:00",
};

const browseItems: BrowseFileInfo[] = [
  bf("file1.txt", 100, false),
  bf("file2.txt", 200, false),
  bf("subdir", 0, true),
];

const subfolderItems: BrowseFileInfo[] = [
  bf("deep.txt", 50, false),
];

function bf(name: string, size: number, isDirectory: boolean, path = name): BrowseFileInfo {
  return { name, size, is_directory: isDirectory, is_dir: isDirectory, path, modified_at: 1_700_000_000_000 };
}

function browseResponse(items: BrowseFileInfo[], total = items.length) {
  return { items, total, page: 1, page_size: 200 };
}


function listResponse(files: FileInfo[], total: number): FileListResponse {
  return {
    files,
    total,
    space: { used: 1024, frozen: 0, available: 9216 },
  };
}

async function renderAndWait(files: FileInfo[] = [folderFile, alphaFile, betaFile], total = files.length) {
  mockApi.listFiles.mockResolvedValue(listResponse(files, total));
  render(
    <ToastProvider>
      <FilesPage />
    </ToastProvider>
  );
  await waitFor(() => {
    expect(screen.getByText("MyFolder")).toBeInTheDocument();
  });
}

async function enterFolder() {
  mockApi.browseFile.mockResolvedValue(browseResponse(browseItems));
  fireEvent.click(screen.getByRole("button", { name: "MyFolder" }));
  await waitFor(() => {
    expect(screen.getByText("file1.txt")).toBeInTheDocument();
  });
}

async function openSubfolder() {
  mockApi.browseFile.mockResolvedValue(browseResponse(subfolderItems));
  fireEvent.click(screen.getByRole("button", { name: "打开" }));
  await waitFor(() => {
    expect(screen.getByText("deep.txt")).toBeInTheDocument();
  });
}

async function runQuery(keyword: string) {
  const input = screen.getByRole("textbox", { name: "搜索文件" });
  fireEvent.change(input, { target: { value: keyword } });
  fireEvent.keyDown(input, { key: "Enter" });
  return screen.findByRole("dialog", { name: "搜索结果" });
}

function rootTableOrder() {
  return document.querySelector(".file-table-wrapper")?.textContent ?? "";
}

beforeEach(() => {
  jest.clearAllMocks();
  jest.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  (console.error as jest.Mock).mockRestore?.();
});

describe("FilesPage root list", () => {
  test("listFiles failure renders the error message", async () => {
    mockApi.listFiles.mockRejectedValue(new Error("网络错误"));

    render(
      <ToastProvider>
        <FilesPage />
      </ToastProvider>
    );

    expect(await screen.findByText("网络错误")).toBeInTheDocument();
  });

  test.each([
    ["名称", "alpha.txt", "beta.txt"],
    ["大小", "alpha.txt", "beta.txt"],
    ["时间", "alpha.txt", "beta.txt"],
  ] as const)(
    "clicking %s header toggles between the two sort directions",
    async (header, firstClickFirst, firstClickSecond) => {
      await renderAndWait();

      fireEvent.click(screen.getByRole("button", { name: new RegExp(header) }));
      const first = rootTableOrder();
      expect(first.indexOf(firstClickFirst)).toBeLessThan(first.indexOf(firstClickSecond));

      fireEvent.click(screen.getByRole("button", { name: new RegExp(header) }));
      const second = rootTableOrder();
      expect(second.indexOf(firstClickFirst)).toBeGreaterThan(second.indexOf(firstClickSecond));
    }
  );

  test("directories stay first regardless of sort order", async () => {
    await renderAndWait();

    fireEvent.click(screen.getByRole("button", { name: /名称/ }));
    const order = rootTableOrder();
    expect(order.indexOf("MyFolder")).toBeLessThan(order.indexOf("alpha.txt"));
  });

  test("individual checkbox toggles selection on and off", async () => {
    await renderAndWait();

    // 桌面视图行复选框无独立名称：全局[0]、表头全选[1]、MyFolder[2]、beta[3]、alpha[4]
    fireEvent.click(screen.getAllByRole("checkbox")[4]);
    expect(screen.getByText(/已选 1 项/)).toBeInTheDocument();

    fireEvent.click(screen.getAllByRole("checkbox")[4]);
    expect(screen.queryByText(/已选 1 项/)).not.toBeInTheDocument();
  });

  test("select all twice clears the selection", async () => {
    await renderAndWait();

    fireEvent.click(screen.getByRole("button", { name: "全选" }));
    expect(screen.getByText(/已选 3 项/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "取消全选" }));
    expect(screen.queryByText(/已选/)).not.toBeInTheDocument();
  });
});

describe("FilesPage rename", () => {
  /** 行序默认为 [MyFolder, beta, alpha]，alpha 的操作按钮在各组末位 */
  async function startRename() {
    fireEvent.click(screen.getAllByRole("button", { name: "重命名" })[2]);
    return await screen.findByLabelText("重命名文件");
  }

  test("empty new name warns and skips the api call", async () => {
    await renderAndWait();

    const input = await startRename();
    fireEvent.change(input, { target: { value: "   " } });
    fireEvent.click(screen.getByRole("button", { name: "确认重命名" }));

    expect(await screen.findByText(/请输入新名称/)).toBeInTheDocument();
    expect(mockApi.renameFile).not.toHaveBeenCalled();
  });

  test("successful rename reloads the list", async () => {
    await renderAndWait();

    const input = await startRename();
    fireEvent.change(input, { target: { value: "renamed.txt" } });
    fireEvent.click(screen.getByRole("button", { name: "确认重命名" }));

    await waitFor(() => {
      expect(mockApi.renameFile).toHaveBeenCalledWith("hash_alpha", "renamed.txt");
    });
    await waitFor(() => {
      expect(mockApi.listFiles).toHaveBeenCalledTimes(2);
    });
  });

  test("rename failure shows error toast and stays in edit mode", async () => {
    mockApi.renameFile.mockRejectedValue(new Error("名称已存在"));

    await renderAndWait();

    const input = await startRename();
    fireEvent.change(input, { target: { value: "dup.txt" } });
    fireEvent.click(screen.getByRole("button", { name: "确认重命名" }));

    expect(await screen.findByText(/重命名失败: 名称已存在/)).toBeInTheDocument();
  });

  test("cancel rename via button and Escape restores the row", async () => {
    await renderAndWait();

    await startRename();
    fireEvent.click(screen.getByRole("button", { name: "取消重命名" }));
    expect(screen.queryByLabelText("重命名文件")).not.toBeInTheDocument();

    await startRename();
    fireEvent.keyDown(screen.getByLabelText("重命名文件"), { key: "Escape" });
    expect(screen.queryByLabelText("重命名文件")).not.toBeInTheDocument();
  });
});

describe("FilesPage download and share", () => {
  test("root download triggers downloadFileUrl with the file hash", async () => {
    mockApi.downloadFileUrl.mockImplementation((fileHash: string) => `http://test/dl/${fileHash}`);

    await renderAndWait();

    fireEvent.click(screen.getAllByRole("button", { name: "下载" })[1]);

    await waitFor(() => {
      expect(mockApi.downloadFileUrl).toHaveBeenCalledWith("hash_alpha", undefined);
    });
    await waitFor(() => {
      expect(screen.getAllByRole("button", { name: "下载" })[1]).toBeEnabled();
    });
  });

  test("download inside a folder passes the subpath", async () => {
    mockApi.downloadFileUrl.mockImplementation(
      (fileHash: string, path?: string) => `http://test/dl/${fileHash}/${path}`
    );
    await renderAndWait();
    await enterFolder();
    await openSubfolder();

    fireEvent.click(screen.getAllByRole("button", { name: "下载" })[0]);

    await waitFor(() => {
      expect(mockApi.downloadFileUrl).toHaveBeenCalledWith("hash_folder", "subdir/deep.txt");
    });
  });

  test("share button opens the dialog and close resets it", async () => {
    await renderAndWait();

    fireEvent.click(screen.getAllByRole("button", { name: "分享" })[2]);

    expect(await screen.findByText("share-dialog:alpha.txt")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "关闭分享" }));
    expect(screen.queryByText("share-dialog:alpha.txt")).not.toBeInTheDocument();
  });
});

describe("FilesPage delete", () => {
  test("deleting a folder asks with the folder-specific message", async () => {
    mockApi.deleteFiles.mockResolvedValue({
      accepted_count: 1,
      failed_count: 0,
      results: [{ content_hash: "hash_folder", ok: true, state: "pending", accepted: true, error: null }],
    });

    await renderAndWait();

    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/删除文件夹 "MyFolder"/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));

    await waitFor(() => {
      expect(mockApi.deleteFiles).toHaveBeenCalledWith(["hash_folder"]);
    });
    await waitFor(() => {
      expect(mockApi.listFiles).toHaveBeenCalledTimes(2);
    });
  });

  test("delete request rejection shows error toast", async () => {
    mockApi.deleteFiles.mockRejectedValue(new Error("服务不可用"));

    await renderAndWait();

    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[2]);
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));

    expect(await screen.findByText(/删除失败: 服务不可用/)).toBeInTheDocument();
  });

  test("batch delete partial failure shows warning toast", async () => {
    mockApi.deleteFiles.mockResolvedValue({
      accepted_count: 2,
      failed_count: 1,
      results: [],
    });

    await renderAndWait();

    fireEvent.click(screen.getByRole("button", { name: "全选" }));
    fireEvent.click(screen.getByRole("button", { name: "批量删除" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));

    expect(await screen.findByText(/已受理 2 个文件，1 个删除失败/)).toBeInTheDocument();
    await waitFor(() => {
      expect(mockApi.listFiles).toHaveBeenCalledTimes(2);
    });
  });

  test("batch delete falls back a page when the last page is emptied", async () => {
    mockApi.listFiles.mockResolvedValueOnce(listResponse([alphaFile], 11));
    mockApi.listFiles.mockResolvedValueOnce(listResponse([betaFile], 11));
    mockApi.listFiles.mockResolvedValueOnce(listResponse([alphaFile], 10));
    mockApi.deleteFiles.mockResolvedValue({
      accepted_count: 1,
      failed_count: 0,
      results: [{ content_hash: "hash_beta", ok: true, state: "pending", accepted: true, error: null }],
    });

    render(
      <ToastProvider>
        <FilesPage />
      </ToastProvider>
    );
    await waitFor(() => {
      expect(screen.getByText("alpha.txt")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    await waitFor(() => {
      expect(screen.getByText("beta.txt")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "全选" }));
    fireEvent.click(screen.getByRole("button", { name: "批量删除" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));

    await waitFor(() => {
      expect(mockApi.listFiles).toHaveBeenLastCalledWith(1, 10);
    });
  });

  test("batch delete rejection shows error toast", async () => {
    mockApi.deleteFiles.mockRejectedValue(new Error("服务器错误"));

    await renderAndWait();

    fireEvent.click(screen.getByRole("button", { name: "全选" }));
    fireEvent.click(screen.getByRole("button", { name: "批量删除" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));

    expect(await screen.findByText(/删除失败: 服务器错误/)).toBeInTheDocument();
  });


});

describe("FilesPage pack dialog", () => {
  test("pack flow computes size and space then creates the task", async () => {
    mockApi.calculatePackSize.mockResolvedValue({ total_size: 150 } as never);
    mockApi.getAvailableSpace.mockResolvedValue({ available: 9216 } as never);
    mockApi.createPackTask.mockResolvedValue({ id: 9 } as never);

    await renderAndWait();

    fireEvent.click(screen.getAllByRole("checkbox")[4]);
    fireEvent.click(screen.getByRole("button", { name: "打包" }));

    const dialog = await screen.findByRole("dialog", { name: "创建打包任务" });
    expect(await within(dialog).findByText(/预估大小/)).toBeInTheDocument();
    expect(within(dialog).getByText(/可用空间/)).toBeInTheDocument();

    fireEvent.change(within(dialog).getByLabelText("输出文件名"), {
      target: { value: "custom.zip" },
    });
    fireEvent.click(within(dialog).getByLabelText("打包后删除源文件"));
    fireEvent.click(within(dialog).getByRole("button", { name: "确认打包" }));

    await waitFor(() => {
      expect(mockApi.createPackTask).toHaveBeenCalledWith([alphaFile.id], "custom.zip", true);
    });
    expect(await screen.findByText(/打包任务已创建/)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "创建打包任务" })).not.toBeInTheDocument();
    });
    expect(screen.queryByText(/已选/)).not.toBeInTheDocument();
  });

  test("pack info failure toasts and closes the dialog", async () => {
    mockApi.calculatePackSize.mockRejectedValue(new Error("超时"));

    await renderAndWait();

    fireEvent.click(screen.getAllByRole("checkbox")[4]);
    fireEvent.click(screen.getByRole("button", { name: "打包" }));

    expect(await screen.findByText(/获取信息失败: 超时/)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "创建打包任务" })).not.toBeInTheDocument();
    });
  });

  test("pack confirm failure keeps the dialog and toasts", async () => {
    mockApi.calculatePackSize.mockResolvedValue({ total_size: 150 } as never);
    mockApi.getAvailableSpace.mockResolvedValue({ available: 9216 } as never);
    mockApi.createPackTask.mockRejectedValue(new Error("空间不足"));

    await renderAndWait();

    fireEvent.click(screen.getAllByRole("checkbox")[4]);
    fireEvent.click(screen.getByRole("button", { name: "打包" }));

    const dialog = await screen.findByRole("dialog", { name: "创建打包任务" });
    await within(dialog).findByText(/预估大小/);
    fireEvent.click(within(dialog).getByRole("button", { name: "确认打包" }));

    expect(await screen.findByText(/创建打包任务失败: 空间不足/)).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "创建打包任务" })).toBeInTheDocument();
  });

  test("pack task completion reloads the list", async () => {
    await renderAndWait();

    fireEvent.click(screen.getByRole("button", { name: "触发打包完成" }));

    await waitFor(() => {
      expect(mockApi.listFiles).toHaveBeenCalledTimes(2);
    });
  });
});

describe("FilesPage folder browsing errors and selection", () => {
  test("entering a folder that fails resets to the root list", async () => {
    await renderAndWait();
    mockApi.browseFile.mockRejectedValue(new Error("无法读取"));

    fireEvent.click(screen.getByRole("button", { name: "MyFolder" }));

    expect(await screen.findByText(/打开文件夹失败: 无法读取/)).toBeInTheDocument();
    expect(screen.getByText("alpha.txt")).toBeInTheDocument();
    expect(screen.queryByText("file1.txt")).not.toBeInTheDocument();
  });

  test("opening a subfolder that fails toasts an error", async () => {
    await renderAndWait();
    await enterFolder();

    mockApi.browseFile.mockRejectedValue(new Error("损坏的压缩包"));
    fireEvent.click(screen.getByRole("button", { name: "打开" }));

    expect(await screen.findByText(/打开文件夹失败: 损坏的压缩包/)).toBeInTheDocument();
    expect(screen.getByText("file1.txt")).toBeInTheDocument();
  });

  test("breadcrumb navigation failure toasts an error", async () => {
    await renderAndWait();
    await enterFolder();
    await openSubfolder();

    mockApi.browseFile.mockRejectedValue(new Error("IO 错误"));
    fireEvent.click(screen.getByRole("button", { name: "MyFolder" }));

    expect(await screen.findByText(/导航失败: IO 错误/)).toBeInTheDocument();
  });

  test("browse checkbox toggles a single item selection", async () => {
    await renderAndWait();
    await enterFolder();

    // 全局[0]、表头全选[1]、行[2..]（默认名称降序：subdir、file2、file1）
    fireEvent.click(screen.getAllByRole("checkbox")[3]);
    expect(screen.getByText(/已选 1 项/)).toBeInTheDocument();

    fireEvent.click(screen.getAllByRole("checkbox")[3]);
    expect(screen.queryByText(/已选 1 项/)).not.toBeInTheDocument();
  });

  test("browse select all then cancel clears the selection", async () => {
    await renderAndWait();
    await enterFolder();

    fireEvent.click(screen.getByRole("button", { name: "全选" }));
    expect(screen.getByText(/已选 3 项/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "取消全选" }));
    expect(screen.queryByText(/已选/)).not.toBeInTheDocument();
  });

  test("browse sorting by size orders directories first and files by size", async () => {
    await renderAndWait();
    await enterFolder();

    fireEvent.click(screen.getByRole("button", { name: /大小/ }));
    const order = screen.getByTestId("file-list").textContent ?? "";
    expect(order.indexOf("subdir")).toBeLessThan(order.indexOf("file2.txt"));
    expect(order.indexOf("file2.txt")).toBeLessThan(order.indexOf("file1.txt"));

    fireEvent.click(screen.getByRole("button", { name: /大小/ }));
    const ascOrder = screen.getByTestId("file-list").textContent ?? "";
    expect(ascOrder.indexOf("file1.txt")).toBeLessThan(ascOrder.indexOf("file2.txt"));
  });
});

describe("FilesPage search locate branches", () => {
  test("scoped search inside a subfolder sends scopeContentHash and scopePath", async () => {
    mockApi.searchFiles.mockResolvedValue({ items: [], total: 0, truncated: false } satisfies FileSearchResponse);

    await renderAndWait();
    await enterFolder();
    await openSubfolder();
    await runQuery("deep");

    await waitFor(() => {
      expect(mockApi.searchFiles).toHaveBeenCalledWith({
        q: "deep",
        scopeContentHash: "hash_folder",
        scopePath: "subdir",
      });
    });
  });

  test("locating an inner item missing from the folder toasts and keeps the dialog", async () => {
    const ghost: FileSearchItem = {
      user_file_id: 5,
      content_hash: "hash_folder",
      name: "ghost.txt",
      size: 10,
      path: "/MyFolder/sub/ghost.txt",
      is_directory: false,
      entry_path: "sub/ghost.txt",
      rank: 0,
      root_index: 0,
    };
    mockApi.searchFiles.mockResolvedValue({ items: [ghost], total: 1, truncated: false } satisfies FileSearchResponse);
    mockApi.browseFile.mockResolvedValue(browseResponse([bf("other.txt", 1, false)]));

    await renderAndWait();
    const dialog = await runQuery("ghost");

    fireEvent.click(within(dialog).getByRole("button", { name: "定位" }));

    expect(await screen.findByText(/定位失败：未在文件夹中找到该文件/)).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "搜索结果" })).toBeInTheDocument();
  });

  test("locating an inner item in the currently open folder keeps its name", async () => {
    const inner: FileSearchItem = {
      user_file_id: 5,
      content_hash: "hash_folder",
      name: "inner.txt",
      size: 50,
      path: "/MyFolder/sub/inner.txt",
      is_directory: false,
      entry_path: "sub/inner.txt",
      rank: 0,
      root_index: 0,
    };
    mockApi.searchFiles.mockResolvedValue({ items: [inner], total: 1, truncated: false } satisfies FileSearchResponse);

    await renderAndWait();
    await enterFolder();
    mockApi.browseFile.mockResolvedValue(browseResponse([bf("inner.txt", 50, false)]));
    const dialog = await runQuery("inner");

    fireEvent.click(within(dialog).getByRole("button", { name: "定位" }));

    await waitFor(() => {
      expect(mockApi.browseFile).toHaveBeenLastCalledWith("hash_folder", "sub", 1, 200);
    });
    // 同一文件夹内定位：面包屑根名保持当前文件夹名
    expect(screen.getByRole("button", { name: "MyFolder" })).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "搜索结果" })).not.toBeInTheDocument();
    });
  });

  test("locating a root item while inside a folder returns to root and highlights", async () => {
    const rootItem: FileSearchItem = {
      user_file_id: alphaFile.id,
      content_hash: alphaFile.content_hash,
      name: alphaFile.name,
      size: alphaFile.size,
      path: alphaFile.name,
      is_directory: false,
      entry_path: null,
      rank: 0,
      root_index: 1,
    };
    mockApi.searchFiles.mockResolvedValue({ items: [rootItem], total: 1, truncated: false } satisfies FileSearchResponse);

    await renderAndWait();
    await enterFolder();
    const dialog = await runQuery("alpha");

    fireEvent.click(within(dialog).getByRole("button", { name: "定位" }));

    await waitFor(() => {
      expect(screen.getByText("alpha.txt")).toBeInTheDocument();
    });
    expect(screen.queryByText("file1.txt")).not.toBeInTheDocument();
    const highlighted = document.querySelector(".file-locate-highlight");
    expect(highlighted?.textContent).toContain("alpha.txt");
  });

  test("locating a root item absent from the current page toasts", async () => {
    const missing: FileSearchItem = {
      user_file_id: 999,
      content_hash: "hash_missing",
      name: "missing.txt",
      size: 10,
      path: "missing.txt",
      is_directory: false,
      entry_path: null,
      rank: 0,
      root_index: 1,
    };
    mockApi.searchFiles.mockResolvedValue({ items: [missing], total: 1, truncated: false } satisfies FileSearchResponse);

    await renderAndWait();
    const dialog = await runQuery("missing");

    fireEvent.click(within(dialog).getByRole("button", { name: "定位" }));

    expect(await screen.findByText(/定位失败：未在当前列表找到该文件/)).toBeInTheDocument();
  });

  test("pending root locate that misses after a page change toasts", async () => {
    const paged: FileSearchItem = {
      user_file_id: 999,
      content_hash: "hash_missing",
      name: "missing.txt",
      size: 10,
      path: "missing.txt",
      is_directory: false,
      entry_path: null,
      rank: 0,
      root_index: 10,
    };
    mockApi.searchFiles.mockResolvedValue({ items: [paged], total: 1, truncated: false } satisfies FileSearchResponse);
    mockApi.listFiles.mockResolvedValueOnce(listResponse([folderFile, alphaFile, betaFile], 11));
    mockApi.listFiles.mockResolvedValueOnce(listResponse([folderFile, alphaFile, betaFile], 11));

    render(
      <ToastProvider>
        <FilesPage />
      </ToastProvider>
    );
    await waitFor(() => {
      expect(screen.getByText("MyFolder")).toBeInTheDocument();
    });

    const dialog = await runQuery("missing");
    fireEvent.click(within(dialog).getByRole("button", { name: "定位" }));

    await waitFor(() => {
      expect(mockApi.listFiles).toHaveBeenLastCalledWith(2, 10);
    });
    expect(await screen.findByText(/定位失败：未在当前列表找到该文件/)).toBeInTheDocument();
  });

});

describe("FilesPage remaining branches", () => {
  test("locating an inner item at the folder root passes no path", async () => {
    const topInner: FileSearchItem = {
      user_file_id: 5,
      content_hash: "hash_folder",
      name: "top.txt",
      size: 10,
      path: "",
      is_directory: false,
      entry_path: "top.txt",
      rank: 0,
      root_index: 0,
    };
    mockApi.searchFiles.mockResolvedValue({ items: [topInner], total: 1, truncated: false } satisfies FileSearchResponse);
    mockApi.browseFile.mockResolvedValue(browseResponse([bf("top.txt", 10, false)]));

    await renderAndWait();
    const dialog = await runQuery("top");

    fireEvent.click(within(dialog).getByRole("button", { name: "定位" }));

    // 无父路径时 browseFile 不带 path，且路径首段缺失时回退到条目名
    await waitFor(() => {
      expect(mockApi.browseFile).toHaveBeenCalledWith("hash_folder", undefined, 1, 200);
    });
    await waitFor(() => {
      expect(screen.getAllByText("top.txt").length).toBeGreaterThan(0);
    });
  });

  test("single delete with null error falls back to 未知错误", async () => {
    mockApi.deleteFiles.mockResolvedValue({
      accepted_count: 0,
      failed_count: 1,
      results: [{ content_hash: "hash_alpha", ok: false, state: "failed", accepted: false, error: null }],
    });

    await renderAndWait();

    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[2]);
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "删除" }));

    expect(await screen.findByText(/删除失败：未知错误/)).toBeInTheDocument();
  });

  test.each([
    ["single row delete", 2],
    ["batch delete", -1],
  ] as const)("%s cancelled in the confirm dialog performs no request", async (_name, deleteIndex) => {
    await renderAndWait();

    if (deleteIndex < 0) {
      fireEvent.click(screen.getByRole("button", { name: "全选" }));
      fireEvent.click(screen.getByRole("button", { name: "批量删除" }));
    } else {
      fireEvent.click(screen.getAllByRole("button", { name: "删除" })[deleteIndex]);
    }

    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(mockApi.deleteFiles).not.toHaveBeenCalled();
  });

  test("download url failure shows error toast", async () => {
    mockApi.downloadFileUrl.mockImplementation(() => {
      throw new Error("签名失败");
    });

    await renderAndWait();

    fireEvent.click(screen.getAllByRole("button", { name: "下载" })[1]);

    expect(await screen.findByText(/下载失败: 签名失败/)).toBeInTheDocument();
  });

  test("pack dialog cancel and close reset the dialog state", async () => {
    mockApi.calculatePackSize.mockResolvedValue({ total_size: 150 } as never);
    mockApi.getAvailableSpace.mockResolvedValue({ available: 9216 } as never);

    await renderAndWait();

    fireEvent.click(screen.getAllByRole("checkbox")[4]);
    fireEvent.click(screen.getByRole("button", { name: "打包" }));

    const dialog = await screen.findByRole("dialog", { name: "创建打包任务" });
    await within(dialog).findByText(/预估大小/);
    fireEvent.click(within(dialog).getByRole("button", { name: "取消" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "创建打包任务" })).not.toBeInTheDocument();
    });
    expect(mockApi.createPackTask).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "打包" }));
    const reopened = await screen.findByRole("dialog", { name: "创建打包任务" });
    fireEvent.click(within(reopened).getByRole("button", { name: "关闭打包弹窗" }));

    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "创建打包任务" })).not.toBeInTheDocument();
    });
  });
});
