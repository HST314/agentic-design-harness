import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, inboxQuery, workItemsQuery } from "../../api/queries";

export function InstanceWorkbenchRedirectPage(): React.JSX.Element {
  const { instanceId = "" } = useParams();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const instance = useQuery({
    queryKey: ["instance-workbench-redirect", instanceId],
    queryFn: ({ signal }) => api.instance(instanceId, signal),
    enabled: Boolean(instanceId),
    retry: false,
  });
  const taskId = instance.data?.task_id ?? "";
  const workItems = useQuery({
    ...workItemsQuery(taskId),
    enabled: Boolean(taskId),
  });
  const workItem = workItems.data?.items.find((item) => item.instance_ids.includes(instanceId));

  useEffect(() => {
    if (!workItem) return;
    let active = true;
    void api.viewInstance(instanceId).catch(() => undefined).then(() => {
      if (!active) return;
      void queryClient.invalidateQueries({ queryKey: inboxQuery.queryKey });
      navigate(
        `/tasks/${encodeURIComponent(workItem.task_id)}/work-items/${encodeURIComponent(workItem.work_item_id)}`,
        { replace: true },
      );
    });
    return () => { active = false; };
  }, [instanceId, navigate, queryClient, workItem]);

  if (instance.isError || workItems.isError) {
    return (
      <section className="workbench-page agent-workbench-state" role="alert">
        <strong>无法打开专业工作台</strong>
        <p>当前子任务信息暂时无法读取，请稍后重试。</p>
        {taskId ? <Link className="workbench-secondary-button" to={`/tasks/${encodeURIComponent(taskId)}/board`}>返回看板</Link> : null}
      </section>
    );
  }
  if (workItems.data && !workItem) {
    return (
      <section className="workbench-page agent-workbench-state" role="alert">
        <strong>未找到对应的专业工作台</strong>
        <p>该通知对应的子任务可能已经更新。</p>
        <Link className="workbench-secondary-button" to={`/tasks/${encodeURIComponent(taskId)}/board`}>返回看板</Link>
      </section>
    );
  }
  return <section className="workbench-page agent-workbench-state" role="status">正在打开专业工作台…</section>;
}
