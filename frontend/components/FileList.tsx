"use client";

import { TaskFile } from "@/types";

function formatBytes(value?: number | null) {
  if (value === 0 || value == null || Number.isNaN(value)) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let idx = 0;
  let val = value;
  while (val >= 1024 && idx < units.length - 1) {
    val /= 1024;
    idx += 1;
  }
  return `${val.toFixed(1)} ${units[idx]}`;
}

export default function FileList({ files }: { files: TaskFile[] }) {
  if (!files || files.length === 0)
    return <p className="muted">暂无文件列表。</p>;

  return (
    <div className="card p-0 overflow-hidden">
      <table className="file-table">
        <thead className="file-table-header">
          <tr>
            <th className="file-table-th text-left">文件名</th>
            <th className="file-table-th text-right" style={{ width: 120 }}>大小</th>
            <th className="file-table-th text-right" style={{ width: 100 }}>进度</th>
          </tr>
        </thead>
        <tbody>
          {files.map((file, i) => {
            const progress =
              file.length > 0 ? (file.completed_length / file.length) * 100 : 0;
            return (
              <tr key={i} className="file-table-row">
                <td className="file-table-cell">
                  <div className="break-all">{file.path.split("/").pop()}</div>
                  <div className="muted text-xs">{file.path}</div>
                </td>
                <td className="file-table-cell text-right tabular-nums">
                  {formatBytes(file.length)}
                </td>
                <td className="file-table-cell text-right">
                  {progress.toFixed(1)}%
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
