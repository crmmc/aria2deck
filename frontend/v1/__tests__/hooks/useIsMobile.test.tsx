import { act, render, screen } from "@testing-library/react";
import { useIsMobile } from "@/hooks/useIsMobile";

function Harness() {
  const isMobile = useIsMobile();
  return <div>{isMobile ? "mobile" : "desktop"}</div>;
}

function setInnerWidth(width: number) {
  Object.defineProperty(window, "innerWidth", {
    configurable: true,
    writable: true,
    value: width,
  });
}

describe("useIsMobile", () => {
  test("tracks viewport changes against the mobile breakpoint", () => {
    setInnerWidth(1024);

    render(<Harness />);

    expect(screen.getByText("desktop")).toBeInTheDocument();

    act(() => {
      setInnerWidth(480);
      window.dispatchEvent(new Event("resize"));
    });

    expect(screen.getByText("mobile")).toBeInTheDocument();
  });
});
