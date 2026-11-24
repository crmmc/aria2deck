import { render } from "@testing-library/react";

import { Icon, IconDefinitions, type IconName } from "@/shared/components/Icon";

describe("Icon", () => {
  it("provides the admin symbols used by the demo", () => {
    const names: IconName[] = ["server", "key", "eye", "eye-off"];
    const { container } = render(
      <>
        <IconDefinitions />
        <Icon name="eye-off" />
      </>,
    );

    for (const name of names) {
      expect(container.querySelector(`[id="icon-${name}"]`)).toBeInTheDocument();
    }
    expect(container.querySelector("use")).toHaveAttribute("href", "#icon-eye-off");
  });
});
