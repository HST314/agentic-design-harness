export type RouteName = "tasks" | "inbox" | "settings";

const ROUTES: Record<RouteName, string> = {
  tasks: "/tasks",
  inbox: "/inbox",
  settings: "/settings",
};

export function currentRoute(pathname = window.location.pathname): RouteName {
  const match = (Object.entries(ROUTES) as Array<[RouteName, string]>).find(
    ([, path]) => pathname === path,
  );
  return match?.[0] ?? "tasks";
}

export function navigate(route: RouteName): void {
  const target = ROUTES[route];
  if (window.location.pathname !== target) {
    window.history.pushState({}, "", target);
  }
  window.dispatchEvent(new PopStateEvent("popstate"));
}

export function routePath(route: RouteName): string {
  return ROUTES[route];
}
