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
    <div className="card" style={{ padding: 0, overflow: "hidden" }}>
      <table
        style={{ width: "100%", borderCollapse: "collapse", fontSize: 14 }}
      >
        <thead
          style={{
            background: "rgba(0,0,0,0.03)",
            borderBottom: "1px solid rgba(0,0,0,0.05)",
          }}
        >
          <tr>
            <th
              style={{
                padding: "12px 16px",
                textAlign: "left",
                fontWeight: 600,
                color: "var(--muted)",
              }}
            >
              文件名
            </th>
            <th
              style={{
                padding: "12px 16px",
                textAlign: "right",
                fontWeight: 600,
                color: "var(--muted)",
                width: 120,
              }}
            >
              大小
            </th>
            <th
              style={{
                padding: "12px 16px",
                textAlign: "right",
                fontWeight: 600,
                color: "var(--muted)",
                width: 100,
              }}
            >
              进度
            </th>
          </tr>
        </thead>
        <tbody>
          {files.map((file, i) => {
            const progress =
              file.length > 0 ? (file.completed_length / file.length) * 100 : 0;
            return (
              <tr
                key={i}
                style={{ borderBottom: "1px solid rgba(0,0,0,0.05)" }}
              >
                <td style={{ padding: "12px 16px" }}>
                  <div style={{ wordBreak: "break-all" }}>
                    {file.path.split("/").pop()}
                  </div>
                  <div className="muted" style={{ fontSize: 12 }}>
                    {file.path}
                  </div>
                </td>
                <td
                  style={{
                    padding: "12px 16px",
                    textAlign: "right",
                    fontVariantNumeric: "tabular-nums",
                  }}
                >
                  {formatBytes(file.length)}
                </td>
                <td style={{ padding: "12px 16px", textAlign: "right" }}>
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
