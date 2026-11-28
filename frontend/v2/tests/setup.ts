import "@testing-library/jest-dom";
import { configure } from "@testing-library/react";
import { TextEncoder } from "node:util";
import { webcrypto } from "node:crypto";

// jsdom 不实现 TextEncoder；密码摘要（shared/lib/crypto.ts）在测试环境同样需要它做 UTF-8 编码。
if (typeof globalThis.TextEncoder === "undefined") {
  Object.defineProperty(globalThis, "TextEncoder", {
    value: TextEncoder,
    writable: true,
    configurable: true,
  });
}

// jsdom 不实现 WebCrypto：注入 Node 原生 webcrypto，避免登录/改密测试走纯 JS PBKDF2 回退
//（实测单次 83ms，全量并发 + 覆盖率插桩时放大为 5s 级超时）。
// crypto.test.ts 自行接管 globalThis.crypto 验证两条路径，不受影响。
if (typeof globalThis.crypto?.subtle === "undefined") {
  Object.defineProperty(globalThis, "crypto", {
    value: webcrypto,
    writable: true,
    configurable: true,
  });
}

// findBy*/waitFor 默认 1s 超时对全量并发负载过于敏感；统一放宽到 5s。
configure({ asyncUtilTimeout: 5000 });

// 业务组件测试默认按减少动态效果运行，避免真实 GSAP 帧跨用例残留；动画单测会显式覆盖为 false。
Object.defineProperty(window, "matchMedia", {
  configurable: true,
  writable: true,
  value: jest.fn((query: string) => ({
    matches: query === "(prefers-reduced-motion: reduce)",
    media: query,
  })),
});
