import "@testing-library/jest-dom";
import { TextEncoder } from "node:util";

// jsdom 不实现 TextEncoder；密码摘要（shared/lib/crypto.ts）在测试环境同样需要它做 UTF-8 编码。
if (typeof globalThis.TextEncoder === "undefined") {
  Object.defineProperty(globalThis, "TextEncoder", {
    value: TextEncoder,
    writable: true,
    configurable: true,
  });
}

// 业务组件测试默认按减少动态效果运行，避免真实 GSAP 帧跨用例残留；动画单测会显式覆盖为 false。
Object.defineProperty(window, "matchMedia", {
  configurable: true,
  writable: true,
  value: jest.fn((query: string) => ({
    matches: query === "(prefers-reduced-motion: reduce)",
    media: query,
  })),
});
