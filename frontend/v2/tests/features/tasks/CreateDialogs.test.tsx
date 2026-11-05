import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import type { TorrentPreviewResponse } from "@/shared/api/types";
import { FailureDialog, TorrentDialog, UriCreateDialog } from "@/features/tasks/CreateDialogs";

const PREVIEW: TorrentPreviewResponse = {
  info_hash: "abc",
  name: "ubuntu.iso",
  files: [
    { index: 0, path: "ubuntu.iso", size: 1024 },
    { index: 1, path: "docs/readme.txt", size: 512 },
  ],
  total_size: 1536,
  default_selection: [0, 1],
};

function renderTorrent(onCreate: jest.Mock) {
  render(
    <TorrentDialog preview={PREVIEW} onClose={jest.fn()} onCreate={onCreate} />,
  );
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
    await waitFor(() => expect(onCreate).toHaveBeenCalledWith("https://a\nhttps://b"));
    expect(await screen.findByRole("alert")).toHaveTextContent("链接不被支持");
    // 失败后按钮恢复可点击。
    expect(screen.getByRole("button", { name: /添加/ })).not.toBeDisabled();
  });
});

describe("TorrentDialog", () => {
  function checkboxes(): HTMLInputElement[] {
    return screen.getAllByRole("checkbox") as HTMLInputElement[];
  }

  it("toggles selection and submits sorted indexes", async () => {
    const onCreate = jest.fn().mockResolvedValue(undefined);
    const { unmount } = render(
      <TorrentDialog preview={PREVIEW} onClose={jest.fn()} onCreate={onCreate} />,
    );
    expect(screen.getByText(/已选 2 \/ 2 个文件/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "创建（2 个文件）" }));
    await waitFor(() => expect(onCreate).toHaveBeenCalledWith([0, 1]));
    unmount(); // 成功路径由父组件卸载弹窗。

    const secondCreate = jest.fn().mockResolvedValue(undefined);
    render(
      <TorrentDialog preview={PREVIEW} onClose={jest.fn()} onCreate={secondCreate} />,
    );
    fireEvent.click(checkboxes()[0]);
    expect(screen.getByText(/已选 1 \/ 2 个文件/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /创建（1 个文件）/ }));
    await waitFor(() => expect(secondCreate).toHaveBeenCalledWith([1]));
  });

  it("disables submit with empty selection and shows inline error", async () => {
    const onCreate = jest.fn().mockRejectedValueOnce(new Error("创建失败"));
    renderTorrent(onCreate);
    fireEvent.click(checkboxes()[0]);
    fireEvent.click(checkboxes()[1]);
    expect(screen.getByRole("button", { name: /创建/ })).toBeDisabled();

    fireEvent.click(checkboxes()[0]);
    fireEvent.click(screen.getByRole("button", { name: /创建（1 个文件）/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("创建失败");
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
        onRetryFailed={onRetryFailed}
        onClose={jest.fn()}
      />,
    );
    expect(screen.getByRole("dialog", { name: "2 条任务失败" })).toBeInTheDocument();
    expect(screen.getByText("https://bad.example/x")).toBeInTheDocument();
    expect(screen.getByText("空间不足")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "仅重试失败项" }));
    expect(onRetryFailed).toHaveBeenCalledTimes(1);
  });
});
