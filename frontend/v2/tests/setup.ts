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
