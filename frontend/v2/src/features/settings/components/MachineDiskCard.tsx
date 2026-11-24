import { Icon } from "@/shared/components/Icon";
import type { MachineStatsResponse } from "@/shared/api/types";
import { formatAdminBytes } from "@/shared/lib/format";

/** total 无效时记 0；真实统计不做 100% 上限裁剪。 */
function toPercent(value: number, total: number): number {
  if (!total || total <= 0) return 0;
  return (value / total) * 100;
}

function percentText(value: number): string {
  return `${value.toFixed(1)}%`;
}

type MachineDiskCardProps = {
  stats: MachineStatsResponse;
};

export function MachineDiskCard({ stats }: MachineDiskCardProps) {
  const downloadPercent = toPercent(stats.download_used_bytes, stats.total_bytes);
  const systemPercent = toPercent(stats.system_used_bytes, stats.total_bytes);
  const totalPercent = toPercent(stats.used_bytes, stats.total_bytes);
  const [freeValue, freeUnit] = formatAdminBytes(stats.free_bytes).split(" ");

  return (
    <section className="machine-card" aria-label="机器磁盘空间">
      <div className="machine-heading">
        <div className="machine-heading-main">
          <span className="machine-icon" aria-hidden="true"><Icon name="server" /></span>
          <div>
            <span>机器磁盘空间</span>
            <strong>{freeValue} <small>{freeUnit} 可用</small></strong>
          </div>
        </div>
        <span className="machine-total">共 {formatAdminBytes(stats.total_bytes)}</span>
      </div>
      <div
        className="machine-meter"
        role="meter"
        aria-label={`机器磁盘总占用 ${percentText(totalPercent)}`}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Number(totalPercent.toFixed(1))}
      >
        <span className="machine-system" style={{ width: `${systemPercent}%` }} />
        <span className="machine-download" style={{ width: `${downloadPercent}%` }} />
      </div>
      <div className="machine-legend">
        <span>
          <i className="legend-dot reserved" />
          系统占用 {formatAdminBytes(stats.system_used_bytes)} · {percentText(systemPercent)}
        </span>
        <span>
          <i className="legend-dot" />
          下载占用 {formatAdminBytes(stats.download_used_bytes)} · {percentText(downloadPercent)}
        </span>
        <span>总占用 {formatAdminBytes(stats.used_bytes)} · {percentText(totalPercent)}</span>
      </div>
    </section>
  );
}
