import { useEffect, useRef } from "react";
import {
  BrowserRouter,
  Navigate,
  Route,
  Routes,
} from "react-router-dom";
import { TaskIntakePage } from "../features/task-intake/TaskIntakePage";
import { AgentWorkbenchPage } from "../features/agent-workbench/AgentWorkbenchPage";
import { DeliveryPage } from "../features/deliveries/DeliveryPage";
import { InboxPage } from "../features/inbox/InboxPage";
import { MasterRoutePage } from "../features/master-thread/MasterThreadPage";
import { SystemSettingsPage } from "../features/settings/SystemSettingsPage";
import {
  TaskBoardPage,
  TaskPlanPage,
} from "../features/task-board/TaskBoardPage";
import { AppShell } from "../layout/AppShell";

function LegacyRoute(): React.JSX.Element {
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let dispose: (() => void) | undefined;
    let cancelled = false;
    const element = rootRef.current;
    if (element) {
      void import("../main").then(({ mountLegacyApp }) => {
        if (!cancelled) dispose = mountLegacyApp(element);
      });
    }
    return () => {
      cancelled = true;
      dispose?.();
    };
  }, []);

  return <div className="legacy-route" ref={rootRef} />;
}

export function AppRouter(): React.JSX.Element {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Navigate replace to="/tasks/new" />} />
        <Route element={<AppShell />}>
          <Route path="/tasks/new" element={<TaskIntakePage />} />
          <Route
            path="/tasks/:taskId/master"
            element={<MasterRoutePage />}
          />
          <Route
            path="/tasks/:taskId/board"
            element={<TaskBoardPage />}
          />
          <Route
            path="/tasks/:taskId/plan"
            element={<TaskPlanPage />}
          />
          <Route
            path="/tasks/:taskId/deliveries"
            element={<DeliveryPage />}
          />
          <Route
            path="/tasks/:taskId/work-items/:workItemId"
            element={<AgentWorkbenchPage />}
          />
          <Route path="/settings" element={<SystemSettingsPage />} />
          <Route path="/inbox" element={<InboxPage />} />
        </Route>
        <Route
          path="/tasks/:taskId/work-items/:workItemId/focus"
          element={<AgentWorkbenchPage focusMode />}
        />
        <Route path="*" element={<LegacyRoute />} />
      </Routes>
    </BrowserRouter>
  );
}
