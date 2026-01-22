"use client";

type Props = {
  samples: number[];
  height?: number;
};

function formatBytes(value: number) {
  if (!value) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let idx = 0;
  let val = value;
  while (val >= 1024 && idx < units.length - 1) {
    val /= 1024;
    idx += 1;
  }
  return `${val.toFixed(1)} ${units[idx]}`;
}

export default function SpeedChart({ samples, height = 120 }: Props) {
  const max = Math.max(1, ...samples) * 1.1; // Add 10% headroom
  const width = 600;

  // Create area path (close the loop at the bottom)
  const points = samples
    .map((value, idx) => {
      const x = (idx / Math.max(samples.length - 1, 1)) * width;
      const y = height - (value / max) * height;
      return `${x},${y}`;
    })
    .join(" ");

  const areaPoints = `${points} ${width},${height} 0,${height}`;

  return (
    <div>
      <svg
        width="100%"
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        style={{ overflow: "visible" }}
      >
        <defs>
          <linearGradient id="speedGradient" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#0a84ff" stopOpacity="0.4" />
            <stop offset="100%" stopColor="#0a84ff" stopOpacity="0" />
          </linearGradient>
        </defs>

        {/* Fill Area */}
        <polygon points={areaPoints} fill="url(#speedGradient)" />

        {/* Line */}
        <polyline
          fill="none"
          stroke="#0a84ff"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          points={points}
        />
      </svg>
      <div className="space-between" style={{ marginTop: 8 }}>
        <p className="muted" style={{ fontSize: "12px" }}>
          60秒前
        </p>
        <p
          style={{
            fontWeight: 600,
            color: "#0a84ff",
            fontVariantNumeric: "tabular-nums",
          }}
        >
          {formatBytes(samples[samples.length - 1] || 0)}/s
        </p>
        <p className="muted" style={{ fontSize: "12px" }}>
          当前
        </p>
      </div>
    </div>
  );
}
