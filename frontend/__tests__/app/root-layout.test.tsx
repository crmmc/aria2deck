import { TextEncoder } from "util";
import RootLayout from "@/app/layout";

global.TextEncoder = TextEncoder;

jest.mock("@/components/Providers", () => ({
  __esModule: true,
  Providers: ({ children }: { children: React.ReactNode }) => <>{children}</>,
}));

jest.mock("@/components/LiquidGlassFilter", () => ({
  __esModule: true,
  default: () => null,
}));

describe("RootLayout", () => {
  test("renders loading styles without escaped quotes", () => {
    const { renderToStaticMarkup } = jest.requireActual<typeof import("react-dom/server")>(
      "react-dom/server"
    );
    const html = renderToStaticMarkup(
      <RootLayout>
        <main>content</main>
      </RootLayout>
    );

    expect(html).toContain("#app-loading");
    expect(html).not.toContain("&quot;");
  });
});
