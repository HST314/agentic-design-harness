export type RouteName = "tasks" | "inbox";
export type TaskSection = "resources" | "approvals" | "usage" | "events";
export type Route =
  | { name: RouteName }
  | { name: "task"; taskId: string }
  | { name: "taskSection"; taskId: string; section: TaskSection }
  | { name: "instance"; instanceId: string };

const ROUTES: Record<RouteName, string> = {
  tasks: "/tasks",
  inbox: "/inbox",
};

const IDENTIFIER = /^[A-Za-z][A-Za-z0-9_-]{0,127}$/;

export function currentRoute(pathname = window.location.pathname): Route {
  const section = pathname.match(/^\/tasks\/([^/]+)\/(resources|approvals|usage|events)$/);
  const sectionTaskId = parseIdentifier(section?.[1]);
  if (sectionTaskId && section?.[2]) {
    return {
      name: "taskSection",
      taskId: sectionTaskId,
      section: section[2] as TaskSection,
    };
  }
  const task = pathname.match(/^\/tasks\/([^/]+)$/);
  const taskId = parseIdentifier(task?.[1]);
  if (taskId) {
    return { name: "task", taskId };
  }
  const instance = pathname.match(/^\/instances\/([^/]+)$/);
  const instanceId = parseIdentifier(instance?.[1]);
  if (instanceId) {
    return { name: "instance", instanceId };
  }
  const match = (Object.entries(ROUTES) as Array<[RouteName, string]>).find(
    ([, path]) => pathname === path,
  );
  return { name: match?.[0] ?? "tasks" };
}

function parseIdentifier(value: string | undefined): string | null {
  if (!value) return null;
  try {
    const decoded = decodeURIComponent(value);
    return IDENTIFIER.test(decoded) ? decoded : null;
  } catch {
    return null;
  }
}

export function navigate(route: Route): void {
  const target = routePath(route);
  if (window.location.pathname !== target) {
    window.history.pushState({}, "", target);
  }
  window.dispatchEvent(new PopStateEvent("popstate"));
}

export function routePath(route: Route | RouteName): string {
  const resolved = typeof route === "string" ? { name: route } : route;
  if (resolved.name === "task") return `/tasks/${encodeURIComponent(resolved.taskId)}`;
  if (resolved.name === "taskSection") {
    return `/tasks/${encodeURIComponent(resolved.taskId)}/${resolved.section}`;
  }
  if (resolved.name === "instance") {
    return `/instances/${encodeURIComponent(resolved.instanceId)}`;
  }
  return ROUTES[resolved.name];
}
