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
