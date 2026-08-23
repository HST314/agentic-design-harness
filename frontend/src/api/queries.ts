import { queryOptions } from "@tanstack/react-query";
import { ApiClient } from "./client";

export const api = new ApiClient();

export const taskHistoryQuery = queryOptions({
  queryKey: ["tasks", "navigation"],
  queryFn: ({ signal }) => api.tasks(signal),
  refetchInterval: 10_000,
  refetchIntervalInBackground: false,
});

export const readinessQuery = queryOptions({
  queryKey: ["service", "readiness"],
  queryFn: ({ signal }) => api.readiness(signal),
  refetchInterval: 10_000,
  refetchIntervalInBackground: false,
});

export const taskIntakeQuery = (taskId: string) => queryOptions({
  queryKey: ["task-intake", taskId],
  queryFn: ({ signal }: { signal: AbortSignal }) => api.taskIntake(taskId, signal),
  retry: false,
});

export const masterSessionQuery = (taskId: string) => queryOptions({
  queryKey: ["master-session", taskId],
  queryFn: ({ signal }: { signal: AbortSignal }) => api.masterSession(taskId, signal),
  refetchInterval: (query) => {
    const active = query.state.data?.thread.active_run;
    return active?.status === "SUBMITTING" || active?.status === "RUNNING" ? 3_000 : 5_000;
  },
  refetchIntervalInBackground: false,
  retry: false,
});

export function visiblePollInterval(
  interval: number | undefined,
  visibilityState: DocumentVisibilityState | undefined = (
    typeof document === "undefined" ? undefined : document.visibilityState
  ),
): number | false {
  if (visibilityState === "hidden") return false;
  return interval ?? 5_000;
}

export const workItemsQuery = (taskId: string) => queryOptions({
  queryKey: ["work-items", taskId],
  queryFn: ({ signal }: { signal: AbortSignal }) => api.workItems(taskId, signal),
  refetchInterval: (query) => visiblePollInterval(query.state.data?.refresh_after_ms),
  refetchIntervalInBackground: false,
  retry: false,
});

export const workItemDetailQuery = (taskId: string, workItemId: string) => queryOptions({
  queryKey: ["work-items", taskId, workItemId],
  queryFn: ({ signal }: { signal: AbortSignal }) => api.workItem(taskId, workItemId, signal),
  refetchInterval: (query) => visiblePollInterval(query.state.data?.refresh_after_ms),
  refetchIntervalInBackground: false,
  retry: false,
});

export const agentWorkbenchLinkQuery = (
  taskId: string,
  workItemId: string,
  instanceId: string,
) => queryOptions({
  queryKey: ["agent-workbench", taskId, workItemId, instanceId],
  queryFn: ({ signal }: { signal: AbortSignal }) => api.instanceUiLink(
    taskId,
    workItemId,
    instanceId,
    signal,
  ),
  retry: false,
  staleTime: 15_000,
});

export const deliveryBundlesQuery = (taskId: string) => queryOptions({
  queryKey: ["delivery-bundles", taskId],
  queryFn: ({ signal }: { signal: AbortSignal }) => api.deliveryBundles(taskId, signal),
  refetchInterval: 5_000,
  refetchIntervalInBackground: false,
  retry: false,
});
