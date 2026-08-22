import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, test } from "vitest";
import { ICON_NAMES, Icon } from "./Icon";

describe("Icon", () => {
  test("renders every structural icon as a decorative, token-driven SVG", () => {
    for (const name of ICON_NAMES) {
      const markup = renderToStaticMarkup(<Icon name={name} />);

      expect(markup).toContain("<svg");
      expect(markup).toContain('aria-hidden="true"');
      expect(markup).toContain('viewBox="0 0 24 24"');
      expect(markup).toContain('stroke="currentColor"');
      expect(markup).not.toMatch(/\p{Extended_Pictographic}/u);
    }
  });

  test("keeps the icon catalog unique so accessible button labels stay stable", () => {
    expect(new Set(ICON_NAMES).size).toBe(ICON_NAMES.length);
  });
});
