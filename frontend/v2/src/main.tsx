import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "@/app/App";
import "@/styles/tokens.css";
import "@/styles/layout.css";
import "@/styles/components.css";
import "@/styles/responsive.css";

const root = document.getElementById("root");

if (root === null) {
  throw new Error("应用挂载点不存在");
}

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
