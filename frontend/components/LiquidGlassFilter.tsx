"use client";

import { useEffect, useRef } from "react";

function smoothStep(a: number, b: number, t: number) {
  t = Math.max(0, Math.min(1, (t - a) / (b - a)));
  return t * t * (3 - 2 * t);
}

function lengthVec(x: number, y: number) {
  return Math.sqrt(x * x + y * y);
}

function roundedRectSDF(x: number, y: number, w: number, h: number, r: number) {
  const qx = Math.abs(x) - w + r;
  const qy = Math.abs(y) - h + r;
  return Math.min(Math.max(qx, qy), 0) + lengthVec(Math.max(qx, 0), Math.max(qy, 0)) - r;
}

export default function LiquidGlassFilter({
  id = "liquid-glass",
  width = 1280,
  height = 720,
}) {
  const svgRef = useRef<SVGSVGElement>(null);

  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;

    const canvas = document.createElement("canvas");
    const dpi = 0.5;
    const w = Math.round(width * dpi);
    const h = Math.round(height * dpi);
    canvas.width = w;
    canvas.height = h;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const data = new Uint8ClampedArray(w * h * 4);
    let maxScale = 0;
    const rawValues: number[] = [];

    for (let i = 0; i < w * h; i += 1) {
      const px = i % w;
      const py = Math.floor(i / w);
      const uvx = px / w;
      const uvy = py / h;
      const ix = uvx - 0.5;
      const iy = uvy - 0.5;
      const dist = roundedRectSDF(ix, iy, 0.42, 0.35, 0.15);
      const displacement = smoothStep(0.6, 0, dist - 0.08);
      const scaled = smoothStep(0, 1, displacement);
      const dx = ix * scaled * w - (px - w / 2);
      const dy = iy * scaled * h - (py - h / 2);

      maxScale = Math.max(maxScale, Math.abs(dx), Math.abs(dy));
      rawValues.push(dx, dy);
    }

    maxScale *= 0.5;
    let idx = 0;
    for (let i = 0; i < data.length; i += 4) {
      const r = rawValues[idx++] / maxScale + 0.5;
      const g = rawValues[idx++] / maxScale + 0.5;
      data[i] = r * 255;
      data[i + 1] = g * 255;
      data[i + 2] = 0;
      data[i + 3] = 255;
    }

    ctx.putImageData(new ImageData(data, w, h), 0, 0);

    const feImage = svg.querySelector(`#${id}_map`);
    const feDisp = svg.querySelector(`#${id}_disp`);
    if (feImage && feDisp) {
      feImage.setAttributeNS("http://www.w3.org/1999/xlink", "href", canvas.toDataURL());
      feDisp.setAttribute("scale", (maxScale / dpi).toString());
    }
  }, [id, width, height]);

  return (
    <svg
      ref={svgRef}
      xmlns="http://www.w3.org/2000/svg"
      width="0"
      height="0"
      style={{ position: "absolute", pointerEvents: "none" }}
    >
      <defs>
        <filter
          id={`${id}_filter`}
          filterUnits="userSpaceOnUse"
          colorInterpolationFilters="sRGB"
          x="0"
          y="0"
          width={width.toString()}
          height={height.toString()}
        >
          <feImage id={`${id}_map`} width={width.toString()} height={height.toString()} />
          <feDisplacementMap
            id={`${id}_disp`}
            in="SourceGraphic"
            in2={`${id}_map`}
            xChannelSelector="R"
            yChannelSelector="G"
            scale="0"
          />
        </filter>
      </defs>
    </svg>
  );
}
