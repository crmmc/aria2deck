import { render, screen } from "@testing-library/react";

import { App } from "@/app/App";

describe("V2 application shell", () => {
  it("renders the home page", () => {
    window.history.pushState({}, "", "/");

    render(<App />);

    expect(screen.getByRole("heading", { name: "Aria2Deck" })).toBeInTheDocument();
    expect(screen.getByText("多用户下载管理平台")).toBeInTheDocument();
  });

  it("serves the SPA at a direct route", () => {
    window.history.pushState({}, "", "/tasks");

    render(<App />);

    expect(screen.getByRole("heading", { name: "Aria2Deck" })).toBeInTheDocument();
  });
});
