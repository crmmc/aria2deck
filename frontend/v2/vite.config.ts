import { fileURLToPath, URL } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    // 开发态只跑这一个服务：接口与 WebSocket 都转发给本机后端。
    // 不要加 changeOrigin：后端按 Host 与 Origin 是否同源校验 WebSocket，
    // 改写 Host 会让校验失败。
    proxy: {
      "/api": "http://127.0.0.1:8001",
      "/ws": { target: "http://127.0.0.1:8001", ws: true },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
