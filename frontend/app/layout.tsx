import "./globals.css";
import { Providers } from "@/components/Providers";

export const metadata = {
  title: "aria2 控制器",
  description: "aria2 任务管理器",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="zh-CN">
      <body>
        <Providers>
          <div className="container">{children}</div>
        </Providers>
      </body>
    </html>
  );
}
