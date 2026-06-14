import "./globals.css";
import { Providers } from "@/components/Providers";

export const metadata = {
  title: "aria2 控制器",
  description: "aria2 任务管理器",
};

const loadingStyles = `
#app-loading {
  position: fixed;
  inset: 0;
  z-index: 9999;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  background: #fbfbfd;
  transition: opacity 0.3s ease;
}
#app-loading .spinner {
  width: 32px;
  height: 32px;
  border: 3px solid rgba(0, 113, 227, 0.15);
  border-top-color: #0071e3;
  border-radius: 50%;
  animation: app-spin 0.8s linear infinite;
}
#app-loading .brand {
  margin-top: 16px;
  font-size: 14px;
  color: #86868b;
  font-family: -apple-system, BlinkMacSystemFont, Segoe UI, Roboto, sans-serif;
}
@keyframes app-spin {
  to { transform: rotate(360deg); }
}
`;

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="zh-CN">
      <head>
        <style>{loadingStyles}</style>
      </head>
      <body>
        <div id="app-loading">
          <div className="spinner" />
          <div className="brand">加载中…</div>
        </div>
        <Providers>
          <div className="container">{children}</div>
        </Providers>
      </body>
    </html>
  );
}
