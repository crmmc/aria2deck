import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { act } from "react";
import { MemoryRouter } from "react-router-dom";

import { AppProviders } from "@/app/providers";
import { PackDialog, RenameDialog, ShareCreateDialog } from "@/features/files/FilesDialogs";
import type { PackPrecomputeResponse } from "@/shared/api/types";

const mockFetch = jest.fn();
global.fetch = mockFetch as unknown as typeof fetch;

function json(status: number, body: unknown): unknown {
  return { status, ok: status >= 200 && status < 300, json: () => Promise.resolve(body) };
}

function renderDialog(node: React.ReactElement) {
  return render(<MemoryRouter><AppProviders>{node}</AppProviders></MemoryRouter>);
}

function precompute(total: number, available: number): PackPrecomputeResponse {
  return { items: [], total_size: total, available_bytes: available };
}

function packView(status: string, extra: Record<string, unknown> = {}): unknown {
  return {
    id: 7,
    status,
    step: status === "packing" ? "compressing" : null,
    step_progress: 10,
    step_started_at_ms: null,
    created_at_ms: 1,
    updated_at_ms: 1,
    finished_at_ms: null,
    error_code: null,
    delete_source: false,
    output: null,
    ...extra,
  };
}

describe("RenameDialog", () => {
  it("keeps dialog open and inline error on non-Error rejection", async () => {
    const onConfirm = jest.fn().mockRejectedValueOnce("boom" as unknown as Error);
    renderDialog(<RenameDialog initialName="旧名" onClose={jest.fn()} onConfirm={onConfirm} />);
    fireEvent.click(screen.getByRole("button", { name: "保存" }));
    expect(await screen.findByText("改名失败")).toBeInTheDocument();
    expect(screen.getByRole("dialog", { name: "重命名" })).toBeInTheDocument();
  });

  it("blocks empty names after trim", () => {
    renderDialog(<RenameDialog initialName="旧名" onClose={jest.fn()} onConfirm={jest.fn()} />);
    fireEvent.change(screen.getByLabelText("新名称"), { target: { value: "   " } });
    expect(screen.getByRole("button", { name: "保存" })).toBeDisabled();
  });
});

describe("ShareCreateDialog", () => {
  it("surfaces creation error inline and copy failure toast", async () => {
    const writeText = jest.fn().mockRejectedValue(new Error("denied"));
    Object.assign(navigator, { clipboard: { writeText } });
    let createCount = 0;
    renderDialog(
      <ShareCreateDialog
        fileName="a.bin"
        onClose={jest.fn()}
        onCreate={async () => {
          createCount += 1;
          if (createCount === 1) throw new Error("每文件最多 10 个有效分享");
          return {
            id: 1, code: "c1", file_name: "a.bin", file_size: 1, file_kind: "file",
            status: "active", expires_at_ms: null, max_downloads: null,
            download_count: 0, created_at_ms: 1, has_password: false,
          };
        }}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "创建分享" }));
    expect(await screen.findByText("每文件最多 10 个有效分享")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "创建分享" }));
    await screen.findByRole("dialog", { name: "分享已创建" });
    fireEvent.click(screen.getByRole("button", { name: "复制链接" }));
    expect(await screen.findByText("复制失败，请手动复制")).toBeInTheDocument();
  });

  it("limit toggle sends numeric max downloads only when enabled", async () => {
    const onCreate = jest.fn().mockResolvedValue({
      id: 2, code: "c2", file_name: "b.bin", file_size: 1, file_kind: "file",
      status: "active", expires_at_ms: null, max_downloads: 3,
      download_count: 0, created_at_ms: 1, has_password: false,
    });
    renderDialog(
      <ShareCreateDialog fileName="b.bin" onClose={jest.fn()} onCreate={onCreate} />,
    );
    fireEvent.click(screen.getByLabelText("限制下载次数"));
    fireEvent.change(screen.getByLabelText("最大下载次数"), { target: { value: "3x" } });
    fireEvent.click(screen.getByRole("button", { name: "创建分享" }));
    await waitFor(() =>
      expect(onCreate).toHaveBeenCalledWith({ expires_in_seconds: null, max_downloads: 3, password: null }),
    );
  });
});

describe("PackDialog", () => {
  it("shows precompute failure and closes path", async () => {
    const onClose = jest.fn();
    renderDialog(
      <PackDialog
        selectedIds={[1]}
        onClose={onClose}
        onPrecompute={jest.fn().mockRejectedValue(new Error("来源不存在"))}
        onCreated={jest.fn()}
      />,
    );
    expect(await screen.findByText("来源不存在")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("empty max downloads maps to null and permanent expiry reselect works", async () => {
    const onCreate = jest.fn().mockResolvedValue({
      id: 3, code: "c3", file_name: "c.bin", file_size: 1, file_kind: "file",
      status: "active", expires_at_ms: null, max_downloads: null,
      download_count: 0, created_at_ms: 1, has_password: false,
    });
    renderDialog(
      <ShareCreateDialog fileName="c.bin" onClose={jest.fn()} onCreate={onCreate} />,
    );
    fireEvent.change(screen.getByLabelText("有效期"), { target: { value: "86400" } });
    fireEvent.change(screen.getByLabelText("有效期"), { target: { value: "" } }); // 永久
    fireEvent.click(screen.getByLabelText("限制下载次数"));
    fireEvent.change(screen.getByLabelText("最大下载次数"), { target: { value: "" } }); // 空串→null
    fireEvent.click(screen.getByRole("button", { name: "创建分享" }));
    await waitFor(() =>
      expect(onCreate).toHaveBeenCalledWith({
        expires_in_seconds: null,
        max_downloads: null,
        password: null,
      }),
    );
  });

  it("disables confirm when space is insufficient and allows toggles", async () => {
    renderDialog(
      <PackDialog
        selectedIds={[1, 2]}
        onClose={jest.fn()}
        onPrecompute={jest.fn().mockResolvedValue(precompute(9999, 1))}
        onCreated={jest.fn()}
      />,
    );
    await screen.findByText("空间不足，无法创建打包任务");
    expect(screen.getByRole("button", { name: "确认打包" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("输出文件名"), { target: { value: "自定义.zip" } });
    fireEvent.click(screen.getByLabelText("打包后删除源文件"));
    expect(screen.getByLabelText("打包后删除源文件")).toBeChecked();
  });

  it("cancelled terminal state renders a notice", async () => {
    jest.useFakeTimers();
    try {
      mockFetch.mockReset();
      let polls = 0;
      mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
        if (init?.method === "POST" && url === "/api/v2/packs") {
          return json(201, packView("pending"));
        }
        if (url === "/api/v2/packs/7") {
          polls += 1;
          return json(200, packView(polls === 1 ? "pending" : "cancelled"));
        }
        return json(200, {});
      });
      const { unmount } = renderDialog(
        <PackDialog
          selectedIds={[1]}
          onClose={jest.fn()}
          onPrecompute={jest.fn().mockResolvedValue(precompute(10, 100))}
          onCreated={jest.fn()}
        />,
      );
      await act(async () => {
        await Promise.resolve();
      });
      fireEvent.click(screen.getByRole("button", { name: "确认打包" }));
      await act(async () => {
        await Promise.resolve();
      });
      await act(async () => {
        await jest.advanceTimersByTimeAsync(1100);
      });
      await waitFor(() => expect(screen.getByRole("dialog", { name: "打包" })).toBeInTheDocument());
      await act(async () => {
        await jest.advanceTimersByTimeAsync(1100);
      });
      expect(await screen.findByText("打包已取消")).toBeInTheDocument();
      unmount();
    } finally {
      jest.useRealTimers();
    }
  });

  it("packing state swallows the close button", async () => {
    jest.useFakeTimers();
    try {
      mockFetch.mockReset();
      mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
        if (init?.method === "POST" && url === "/api/v2/packs") {
          return json(201, packView("packing", { step_progress: 20 }));
        }
        return json(200, packView("packing", { step_progress: 20 }));
      });
      const { unmount } = renderDialog(
        <PackDialog
          selectedIds={[1]}
          onClose={jest.fn()}
          onPrecompute={jest.fn().mockResolvedValue(precompute(10, 100))}
          onCreated={jest.fn()}
        />,
      );
      await act(async () => {
        await Promise.resolve();
      });
      fireEvent.click(screen.getByRole("button", { name: "确认打包" }));
      await act(async () => {
        await Promise.resolve();
      });
      expect(await screen.findByText(/压缩中/)).toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "关闭" }));
      expect(screen.getByRole("dialog", { name: "打包" })).toBeInTheDocument(); // 打包中不关闭。
      unmount();
    } finally {
      jest.useRealTimers();
    }
  });

  it("create failure surfaces inline error", async () => {
    mockFetch.mockReset();
    mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
      if (init?.method === "POST" && url === "/api/v2/packs") {
        return json(409, { error: { code: "quota_exceeded", message: "配额不足" } });
      }
      return json(200, {});
    });
    renderDialog(
      <PackDialog
        selectedIds={[1]}
        onClose={jest.fn()}
        onPrecompute={jest.fn().mockResolvedValue(precompute(10, 100))}
        onCreated={jest.fn()}
      />,
    );
    await screen.findByText(/预估/);
    fireEvent.click(screen.getByRole("button", { name: "确认打包" }));
    expect(await screen.findByText("配额不足")).toBeInTheDocument();
  });

  it("polls and renders failed terminal state", async () => {
    jest.useFakeTimers();
    try {
      mockFetch.mockReset();
      let polls = 0;
      mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
        if (init?.method === "POST" && url === "/api/v2/packs") {
          return json(201, packView("pending"));
        }
        if (url === "/api/v2/packs/7") {
          polls += 1;
          return json(
            200,
            packView(polls === 1 ? "pending" : "failed", { error_code: "pack_io_error" }),
          );
        }
        return json(200, {});
      });
      const { unmount } = renderDialog(
        <PackDialog
          selectedIds={[1]}
          onClose={jest.fn()}
          onPrecompute={jest.fn().mockResolvedValue(precompute(10, 100))}
          onCreated={jest.fn()}
        />,
      );
      await act(async () => {
        await Promise.resolve();
      });
      fireEvent.click(screen.getByRole("button", { name: "确认打包" }));
      await act(async () => {
        await Promise.resolve();
      });
      await act(async () => {
        await jest.advanceTimersByTimeAsync(1000);
      });
      expect(await screen.findByText(/排队中/)).toBeInTheDocument();
      await act(async () => {
        await jest.advanceTimersByTimeAsync(1000);
      });
      expect(await screen.findByText(/打包失败：pack_io_error/)).toBeInTheDocument();
      unmount();
    } finally {
      jest.useRealTimers();
    }
  });

  it("polls a deleted pack output and skips the raw failure code", async () => {
    jest.useFakeTimers();
    try {
      mockFetch.mockReset();
      let polls = 0;
      mockFetch.mockImplementation(async (url: string, init?: { method?: string }) => {
        if (init?.method === "POST" && url === "/api/v2/packs") {
          return json(201, packView("pending"));
        }
        if (url === "/api/v2/packs/7") {
          polls += 1;
          return json(
            200,
            packView(polls === 1 ? "pending" : "failed", { error_code: "pack_output_deleted" }),
          );
        }
        return json(200, {});
      });
      const { unmount } = renderDialog(
        <PackDialog
          selectedIds={[1]}
          onClose={jest.fn()}
          onPrecompute={jest.fn().mockResolvedValue(precompute(10, 100))}
          onCreated={jest.fn()}
        />,
      );
      await act(async () => {
        await Promise.resolve();
      });
      fireEvent.click(screen.getByRole("button", { name: "确认打包" }));
      await act(async () => {
        await Promise.resolve();
      });
      await act(async () => {
        await jest.advanceTimersByTimeAsync(1000);
      });
      expect(await screen.findByText(/排队中/)).toBeInTheDocument();
      await act(async () => {
        await jest.advanceTimersByTimeAsync(1000);
      });
      expect(await screen.findByText(/产物已删除/)).toBeInTheDocument();
      expect(screen.queryByText(/打包失败/)).not.toBeInTheDocument();
      expect(screen.queryByText(/pack_output_deleted/)).not.toBeInTheDocument();
      unmount();
    } finally {
      jest.useRealTimers();
    }
  });
});
