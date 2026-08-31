export const BRAND = {
  mark: "DH",
  name: "DesignHarness",
  sidebarTagline: "企业级多智能体设计操作系统",
  fullName: "面向广告全案交付的企业级多智能体设计操作系统",
} as const;

export function applyDocumentBrand(documentLike: { title: string }): void {
  documentLike.title = BRAND.fullName;
}
