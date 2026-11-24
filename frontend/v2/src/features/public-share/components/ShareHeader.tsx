import { formatBytes } from "@/shared/lib/format";

import type { ShareInfoView } from "../model";

type ShareHeaderProps = {
  shareInfo: ShareInfoView;
};

export function ShareHeader({ shareInfo }: ShareHeaderProps) {
  return (
    <div className="row mb-6">
      <div className="text-3xl">{shareInfo.is_directory ? "📁" : "📄"}</div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <h2 className="text-lg mb-1" style={{ wordBreak: "break-all", margin: 0 }}>
          {shareInfo.file_name}
        </h2>
        <p className="muted">{formatBytes(shareInfo.file_size)}</p>
      </div>
    </div>
  );
}
