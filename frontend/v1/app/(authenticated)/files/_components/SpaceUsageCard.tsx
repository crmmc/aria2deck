import { formatBytes } from "@/lib/utils";
import type { SpaceInfo } from "@/types";

function getSpacePercentage(space: SpaceInfo) {
  const total = space.used + space.frozen + space.available;
  if (total === 0) return { used: 0, frozen: 0 };
  return {
    used: (space.used / total) * 100,
    frozen: (space.frozen / total) * 100,
  };
}

function getSpaceColor(percentage: number) {
  if (percentage >= 80) return "var(--danger)";
  if (percentage >= 50) return "var(--warning)";
  return "var(--success)";
}

type SpaceUsageCardProps = {
  space: SpaceInfo;
};

export function SpaceUsageCard({ space }: SpaceUsageCardProps) {
  const percentages = getSpacePercentage(space);
  const totalPercentage = percentages.used + percentages.frozen;

  return (
    <div className="card mb-6">
      <div className="flex-between mb-3">
        <div>
          <h3 className="stats-label">可用空间</h3>
          <div className="flex items-baseline gap-2">
            <span className="stats-value">{formatBytes(space.available)}</span>
            <span className="muted">
              / {formatBytes(space.used + space.frozen + space.available)}
            </span>
          </div>
          {space.frozen > 0 && (
            <div className="text-sm muted mt-1">
              已冻结: {formatBytes(space.frozen)} (下载中)
            </div>
          )}
        </div>
      </div>
      <div className="progress-container" style={{ position: "relative" }}>
        <div
          className="progress-bar"
          style={{
            width: `${totalPercentage}%`,
            background: getSpaceColor(totalPercentage),
          }}
        />
        {space.frozen > 0 && (
          <div
            style={{
              position: "absolute",
              left: `${percentages.used}%`,
              top: 0,
              width: `${percentages.frozen}%`,
              height: "100%",
              background: "repeating-linear-gradient(45deg, transparent, transparent 2px, rgba(255,255,255,0.3) 2px, rgba(255,255,255,0.3) 4px)",
            }}
          />
        )}
      </div>
    </div>
  );
}
