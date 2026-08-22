import { useEffect, useRef } from "react";
import {
  BrowserRouter,
  Navigate,
  Route,
  Routes,
} from "react-router-dom";
import { FoundationPage } from "../features/workbench/FoundationPage";
import { TaskIntakePage } from "../features/task-intake/TaskIntakePage";
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
            element={<FoundationPage view="master" />}
          />
          <Route
            path="/tasks/:taskId/board"
            element={<FoundationPage view="board" />}
          />
          <Route
            path="/tasks/:taskId/plan"
            element={<FoundationPage view="plan" />}
          />
          <Route
            path="/tasks/:taskId/work-items/:workItemId"
            element={<FoundationPage view="work-item" />}
          />
        </Route>
        <Route path="*" element={<LegacyRoute />} />
      </Routes>
    </BrowserRouter>
  );
}
