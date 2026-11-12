import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import type { TorrentPreviewResponse } from "@/shared/api/types";
import {
  FailureDialog,
  TorrentCreateWizard,
  UriCreateDialog,
} from "@/features/tasks/CreateDialogs";

const PREVIEW: TorrentPreviewResponse = {
  info_hash: "abc",
  name: "ubuntu.iso",
  files: [
    { index: 0, path: "ubuntu.iso", size: 1024 },
    { index: 1, path: "docs/readme.txt", size: 512 },
    { index: 2, path: "docs/guide.pdf", size: 2048 },
  ],
  total_size: 3584,
  default_selection: [0, 1, 2],
};

function renderWizard(
  onCreate: jest.Mock,
  onClose: jest.Mock = jest.fn(),
  preview: TorrentPreviewResponse = PREVIEW,
) {
  render(<TorrentCreateWizard preview={preview} onClose={onClose} onCreate={onCreate} />);
}

function checkboxByName(label: string): HTMLInputElement {
  return screen.getByLabelText(label) as HTMLInputElement;
}

describe("UriCreateDialog", () => {
  it("disables submit when textarea empty or whitespace only", () => {
    render(<UriCreateDialog onClose={jest.fn()} onCreate={jest.fn()} />);
    const submit = screen.getByRole("button", { name: /添加/ });
    expect(submit).toBeDisabled();
    fireEvent.change(screen.getByLabelText("下载链接列表"), {
      target: { value: "  \n \n" },
    });
    expect(submit).toBeDisabled();
    expect(screen.getByRole("button", { name: "添加 任务" })).toBeInTheDocument();
  });

  it("submits raw text and surfaces error inline", async () => {
    const onCreate = jest.fn().mockRejectedValueOnce(new Error("链接不被支持"));
    render(<UriCreateDialog onClose={jest.fn()} onCreate={onCreate} />);
    fireEvent.change(screen.getByLabelText("下载链接列表"), {
      target: { value: "https://a\nhttps://b" },
    });
    fireEvent.click(screen.getByRole("button", { name: /添加 2 任务/ }));
    await waitFor(() =>
      expect(onCreate).toHaveBeenCalledWith("https://a\nhttps://b", true),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("链接不被支持");
    // 失败后按钮恢复可点击。
    expect(screen.getByRole("button", { name: /添加/ })).not.toBeDisabled();
  });
});

describe("TorrentCreateWizard（三阶段向导 T8）", () => {
  it("keeps server default selection and shows the selection summary", () => {
    renderWizard(jest.fn());
    expect(screen.getByText(/1\. 上传解析/)).toBeInTheDocument();
    expect(screen.getByText(/2\. 选择文件/)).toBeInTheDocument();
    expect(screen.getByText(/3\. 确认下载/)).toBeInTheDocument();
    expect(screen.getByText(/已选 3 项 · 3\.50 KiB/)).toBeInTheDocument();
    expect(checkboxByName("选择文件 ubuntu.iso")).toBeChecked();
    expect(checkboxByName("选择文件 readme.txt")).toBeChecked();
  });

  it("directory checkbox reflects indeterminate state and toggles children", () => {
    renderWizard(jest.fn());
    const folder = checkboxByName("选择目录 docs");
    // 默认全选：目录为全选态而非半选。
    expect(folder.checked).toBe(true);
    expect(folder.indeterminate).toBe(false);
    fireEvent.click(folder); // 取消整个目录。
    expect(checkboxByName("选择文件 readme.txt")).not.toBeChecked();
    expect(checkboxByName("选择文件 guide.pdf")).not.toBeChecked();
    expect(folder.checked).toBe(false);
    expect(folder.indeterminate).toBe(false);

    fireEvent.click(checkboxByName("选择文件 readme.txt"));
    expect(folder.indeterminate).toBe(true); // 部分选中。
    expect(folder.checked).toBe(false);

    // 从空目录整体勾选：子文件全部选中（toggleFolder 的 add 分支）。
    fireEvent.click(checkboxByName("选择文件 readme.txt")); // 先清回空。
    expect(checkboxByName("选择文件 readme.txt")).not.toBeChecked();
    fireEvent.click(folder);
    expect(checkboxByName("选择文件 readme.txt")).toBeChecked();
    expect(checkboxByName("选择文件 guide.pdf")).toBeChecked();
  });

  it("file filter narrows the tree and empty filter result shows a hint", () => {
    renderWizard(jest.fn());
    fireEvent.change(screen.getByLabelText("搜索文件"), { target: { value: "pdf" } });
    expect(screen.getByLabelText("选择文件 guide.pdf")).toBeInTheDocument();
    expect(screen.queryByLabelText("选择文件 ubuntu.iso")).not.toBeInTheDocument();
    // 目录祖先保留。
    expect(screen.getByLabelText("选择目录 docs")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("搜索文件"), { target: { value: "不存在的名字" } });
    expect(screen.getByText("没有匹配文件")).toBeInTheDocument();
  });

  it("select-all and clear-all reset the whole selection", () => {
    renderWizard(jest.fn());
    fireEvent.click(screen.getByRole("button", { name: "清空" }));
    expect(screen.getByText(/已选 0 项 · 0 B/)).toBeInTheDocument();
    // 从空选重新勾选单个文件。
    fireEvent.click(screen.getByLabelText("选择文件 ubuntu.iso"));
    expect(screen.getByLabelText("选择文件 ubuntu.iso")).toBeChecked();
    expect(screen.getByText(/已选 1 项 · 1\.00 KiB/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "全选" }));
    expect(screen.getByText(/已选 3 项 · 3\.50 KiB/)).toBeInTheDocument();
  });

  it("cancel with a selection asks for confirmation, then closes", () => {
    const onClose = jest.fn();
    renderWizard(jest.fn(), onClose);
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByText(/取消添加任务？/)).toBeInTheDocument();
    // 继续选择：回到选择阶段不关闭。
    fireEvent.click(screen.getByRole("button", { name: "继续选择" }));
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.queryByText(/取消添加任务？/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    fireEvent.click(screen.getByRole("button", { name: "确认取消" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("cancel without a selection closes immediately", () => {
    const onClose = jest.fn();
    renderWizard(jest.fn(), onClose);
    fireEvent.click(screen.getByRole("button", { name: "清空" }));
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/取消添加任务？/)).not.toBeInTheDocument();
  });

  it("confirm stage lists only selected files and submits sorted indexes", async () => {
    const onCreate = jest.fn().mockResolvedValue(undefined);
    renderWizard(onCreate);
    fireEvent.click(checkboxByName("选择文件 ubuntu.iso")); // 取消根文件。
    expect(screen.getByText(/已选 2 项 · 2\.50 KiB/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "下一阶段" }));
    expect(screen.getByText(/确认下载内容：2 个文件 · 2\.50 KiB/)).toBeInTheDocument();
    // 只读树仅显示选中文件。
    expect(screen.getByText("readme.txt")).toBeInTheDocument();
    expect(screen.queryByText("选择文件 ubuntu.iso")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /确认创建（2 项）/ }));
    await waitFor(() => expect(onCreate).toHaveBeenCalledWith([1, 2], true));
  });

  it("confirm stage carries an unchecked strict policy to onCreate", async () => {
    const onCreate = jest.fn().mockResolvedValue(undefined);
    renderWizard(onCreate);
    fireEvent.click(screen.getByRole("button", { name: "下一阶段" }));
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: /确认创建（3 项）/ }));
    await waitFor(() => expect(onCreate).toHaveBeenCalledWith([0, 1, 2], false));
  });

  it("back to select stage keeps selection and submit error shows inline", async () => {
    const onCreate = jest.fn().mockRejectedValueOnce(new Error("创建失败"));
    renderWizard(onCreate);
    fireEvent.click(screen.getByRole("button", { name: "下一阶段" }));
    fireEvent.click(screen.getByRole("button", { name: "上一步" }));
    // 回到选择阶段后再次进入确认并提交。
    fireEvent.click(screen.getByRole("button", { name: "下一阶段" }));
    fireEvent.click(screen.getByRole("button", { name: /确认创建/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("创建失败");
  });

  it("next stage is disabled with an empty selection", () => {
    renderWizard(jest.fn());
    fireEvent.click(screen.getByRole("button", { name: "清空" }));
    expect(screen.getByRole("button", { name: "下一阶段" })).toBeDisabled();
  });

  it("orders folders before files within the same directory", () => {
    const folderFirst: TorrentPreviewResponse = {
      info_hash: "z",
      name: "pack",
      files: [
        { index: 0, path: "pack/docs/a.txt", size: 10 },
        { index: 1, path: "pack/z.bin", size: 20 },
      ],
      total_size: 30,
      default_selection: [0, 1],
    };
    renderWizard(jest.fn(), jest.fn(), folderFirst);
    const rows = Array.from(document.querySelectorAll(".torrent-tree-row"));
    // 根目录 pack 下的目录行先于文件行（buildTree 排序：folder 在前，同层按名）。
    expect(rows[0]).toHaveTextContent("pack");
    expect(rows[1]).toHaveTextContent("docs");
    expect(rows[2]).toHaveTextContent("a.txt");
    expect(rows[3]).toHaveTextContent("z.bin");
  });
});

describe("FailureDialog", () => {
  it("lists failed items and forwards retry-failed action", () => {
    const onRetryFailed = jest.fn();
    render(
      <FailureDialog
        failedItems={[
          { uri: "https://bad.example/x", message: "不支持" },
          { uri: "https://bad.example/y", message: "空间不足" },
        ]}
        allowRename={true}
        onAllowRenameChange={jest.fn()}
        onRetryFailed={onRetryFailed}
        onClose={jest.fn()}
      />,
    );
    expect(screen.getByRole("dialog", { name: "2 条任务失败" })).toBeInTheDocument();
    expect(screen.getByText("https://bad.example/x")).toBeInTheDocument();
    expect(screen.getByText("空间不足")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "仅重试失败项" }));
    expect(onRetryFailed).toHaveBeenCalledTimes(1);
    expect(onRetryFailed.mock.calls[0][1]).toBe(true); // 默认保留自动加后缀策略
  });

  it("keeps an unchecked strict policy when retrying failed items", () => {
    const onRetryFailed = jest.fn();
    render(
      <FailureDialog
        failedItems={[{ uri: "https://bad.example/x", message: "不支持" }]}
        allowRename={false}
        onAllowRenameChange={jest.fn()}
        onRetryFailed={onRetryFailed}
        onClose={jest.fn()}
      />,
    );
    expect(screen.getByRole("checkbox")).not.toBeChecked();
    fireEvent.click(screen.getByRole("button", { name: "仅重试失败项" }));
    expect(onRetryFailed.mock.calls[0][1]).toBe(false); // 严格策略随重提发送
  });
});
