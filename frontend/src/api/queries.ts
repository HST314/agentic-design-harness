import { queryOptions } from "@tanstack/react-query";
import { ApiClient } from "./client";

const api = new ApiClient();

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
