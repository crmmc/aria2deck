import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { TorrentCreateWizard } from "@/app/(authenticated)/tasks/_components/TorrentCreateWizard";
import { api } from "@/lib/api";
import type { TorrentPreview } from "@/types";

const uploadTorrent = api.uploadTorrent as jest.Mock;

jest.mock("@/lib/api", () => ({
  api: {
    uploadTorrent: jest.fn(),
  },
}));

const preview = {
  info_hash: "abc123",
  name: "demo",
  file_count: 1,
  total_size: 1024,
  files: [{ index: 1, path: ["demo.bin"], size: 1024 }],
  tree: [{ type: "file", index: 1, path: ["demo.bin"], size: 1024, name: "demo.bin" }],
  limits: { max_files: 1000 },
  default_selection: "all",
} as unknown as TorrentPreview;

function renderWizard() {
  return render(
    <TorrentCreateWizard
      torrentBase64="dG9ycmVudA=="
      preview={preview}
      onCancel={jest.fn()}
      onCreated={jest.fn()}
      onError={jest.fn()}
    />
  );
}

describe("TorrentCreateWizard 提交错误提示", () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  test("重复任务等提交失败时，向导内显示错误横幅", async () => {
    uploadTorrent.mockRejectedValue(new Error("任务已存在"));
    renderWizard();

    // 进入确认阶段
    fireEvent.click(screen.getByRole("button", { name: "下一阶段" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认" }));

    const banner = await screen.findByRole("alert");
    expect(banner).toHaveTextContent("任务已存在");
    // 失败后按钮恢复可点，向导未关闭
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "确认" })).not.toBeDisabled()
    );
  });

  test("提交成功不显示横幅", async () => {
    uploadTorrent.mockResolvedValue({ id: 9 } as never);
    const onCreated = jest.fn();
    render(
      <TorrentCreateWizard
        torrentBase64="dG9ycmVudA=="
        preview={preview}
        onCancel={jest.fn()}
        onCreated={onCreated}
        onError={jest.fn()}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: "下一阶段" }));
    fireEvent.click(await screen.findByRole("button", { name: "确认" }));

    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});

describe("TorrentCreateWizard 文件名悬浮提示", () => {
  test("文件名和目录名省略时，title 属性提供完整名称", () => {
    const longFileName = "a".repeat(80) + ".mkv";
    const longDirName = "b".repeat(80);
    const treePreview = {
      ...preview,
      file_count: 1,
      files: [{ index: 1, path: [longDirName, longFileName], size: 1024 }],
      tree: [
        {
          type: "directory",
          name: longDirName,
          path: [longDirName],
          size: 1024,
          children: [
            { type: "file", index: 1, path: [longDirName, longFileName], size: 1024, name: longFileName },
          ],
        },
      ],
    } as unknown as TorrentPreview;

    render(
      <TorrentCreateWizard
        torrentBase64="dG9ycmVudA=="
        preview={treePreview}
        onCancel={jest.fn()}
        onCreated={jest.fn()}
        onError={jest.fn()}
      />
    );

    expect(screen.getByText(longFileName)).toHaveAttribute("title", longFileName);
    expect(screen.getByText(longDirName)).toHaveAttribute("title", longDirName);
  });
});
