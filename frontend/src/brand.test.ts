import { describe, expect, it } from "vitest";
import { applyDocumentBrand, BRAND } from "./brand";

describe("DesignHarness brand", () => {
  it("keeps the visible and formal product names in one contract", () => {
    expect(BRAND).toEqual({
      mark: "DH",
      name: "DesignHarness",
      sidebarTagline: "企业级多智能体设计操作系统",
      fullName: "面向广告全案交付的企业级多智能体设计操作系统",
    });
  });

  it("applies the formal product name to the browser tab", () => {
    const documentLike = { title: "旧标题" };

    applyDocumentBrand(documentLike);

    expect(documentLike.title).toBe(BRAND.fullName);
  });
});
