import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import {
  agentWorkbenchLinkQuery,
  api,
  workItemDetailQuery,
  workItemsQuery,
} from "../../api/queries";
import { ApiError, type AgentWorkbenchLinkResponse } from "../../api/client";
import { Icon } from "../../components/Icon";
import { TaskTabs } from "../master-thread/MasterThreadPage";
import {
  bridgeIdempotencyKey,
  isBridgeHello,
  newBridgeNonce,
  parseBridgeRequest,
  RUNTIME_SETTINGS_BRIDGE_PROTOCOL,
  RUNTIME_SETTINGS_BRIDGE_VERSION,
  type RuntimeSettingsBridgeRequest,
} from "./runtimeSettingsBridge";

type FrameState = "loading" | "ready" | "failed";
type PptAutoLaunchAction = "start" | "recover" | null;

export function pptAutoLaunchAction({
  requested,
  canStart,
  linkStatus,
  retryAllowed,
}: {
  requested: boolean;
  canStart: boolean;
  linkStatus: AgentWorkbenchLinkResponse["link_status"] | undefined;
  retryAllowed: boolean;
}): PptAutoLaunchAction {
  if (!requested) return null;
  if (linkStatus === "START_FAILED" && retryAllowed) return "recover";
  if (canStart && linkStatus === "NO_UI_URL") return "start";
  return null;
}

function operationId(prefix: string): string {
  const suffix = typeof crypto.randomUUID === "function"
    ? crypto.randomUUID().replaceAll("-", "")
    : `${Date.now()}${Math.random().toString(16).slice(2)}`;
  return `${prefix}_${suffix}`.slice(0, 128);
}

async function executeBridgeRequest(
  request: RuntimeSettingsBridgeRequest,
  instanceId: string,
  taskRevision: number | undefined,
  scope: { taskId: string; workItemId: string },
): Promise<unknown> {
  if (request.action === "runtime_settings.get") {
    return api.instanceRuntimeSettings(instanceId);
  }
  if (!Number.isInteger(taskRevision)) {
    throw new Error("当前任务版本已变化，请刷新专业工作台后重试。");
  }
  const envelope = {
    idempotency_key: bridgeIdempotencyKey(request.action, request.request_id),
    actor_type: "human" as const,
    actor_id: "human_operator",
    expected_revision: taskRevision as number,
  };
  if (request.action === "runtime_settings.sync_toggle") {
    return api.setWorkItemSyncToggle(scope.taskId, scope.workItemId, {
      sync_to_peers: Boolean(request.payload.sync_to_peers),
      envelope,
    });
  }
  if (request.action === "runtime_settings.propose") {
    return api.proposeInstanceRuntimeSettings(instanceId, {
      base_revision: Number(request.payload.base_revision),
      overrides: request.payload.overrides as Record<string, unknown>,
      sync_unstarted_image_work_items: Boolean(request.payload.sync_unstarted_image_work_items),
      expected_sync_instance_ids: request.payload.expected_sync_instance_ids as string[],
      envelope,
    });
  }
  return api.confirmInstanceRuntimeSettings(
    instanceId,
    String(request.payload.proposal_id),
    envelope,
  );
}

function WorkbenchFailure({
  link,
  message,
  onRetry,
  retryLabel,
  retryPending,
  taskId,
  workItemId,
  focusMode,
  agentType,
}: {
  link?: AgentWorkbenchLinkResponse;
  message: string;
  onRetry: () => void;
  retryLabel: string;
  retryPending: boolean;
  taskId: string;
  workItemId: string;
  focusMode: boolean;
  agentType: "image" | "ppt";
}): React.JSX.Element {
  const agentLabel = agentType === "image" ? "Image Agent" : "PPT Agent";
  const heading = link?.link_status === "FRAME_BLOCKED"
    ? `${agentLabel} 无法安全内嵌`
    : link?.link_status === "START_FAILED"
      ? `${agentLabel} 启动未完成`
      : link?.link_status === "STARTING"
        ? `${agentLabel} 正在启动`
    : link?.link_status === "NO_UI_URL"
      ? `${agentLabel} 尚未提供工作台`
      : `${agentLabel} 工作台加载失败`;
  return (
    <div className="agent-workbench-error" role="alert">
      <Icon name="status" />
      <div>
        <p className="workbench-eyebrow">专业工作台</p>
        <h2>{heading}</h2>
        <p>{message}</p>
        <div className="agent-workbench-error__actions">
          <button className="workbench-secondary-button" type="button" disabled={retryPending} onClick={onRetry}>
            <Icon name="retry" />{retryPending ? "正在恢复…" : retryLabel}
          </button>
          {link?.ui_url && (!focusMode || link.link_status === "FRAME_BLOCKED") ? link.link_status === "FRAME_BLOCKED" ? (
            <a
              className="workbench-secondary-button"
              href={link.ui_url}
              target="_blank"
              rel="noopener noreferrer"
            >
              <Icon name="external-link" />直接打开原始工作台
            </a>
          ) : (
            <a
              className="workbench-secondary-button"
              href={`/tasks/${encodeURIComponent(taskId)}/work-items/${encodeURIComponent(workItemId)}/focus`}
              target="_blank"
              rel="noopener noreferrer"
            >
              <Icon name="external-link" />在新标签页中尝试
            </a>
          ) : null}
          <Link className="workbench-secondary-button" to={`/tasks/${encodeURIComponent(taskId)}/board`}>
            返回看板
          </Link>
        </div>
      </div>
    </div>
  );
}

export function AgentWorkbenchPage({ focusMode = false }: { focusMode?: boolean }): React.JSX.Element {
  const queryClient = useQueryClient();
  const { taskId = "", workItemId = "" } = useParams();
  const [searchParams, setSearchParams] = useSearchParams();
  const autoLaunchRequested = searchParams.get("start") === "1";
  const detail = useQuery(workItemDetailQuery(taskId, workItemId));
  const workItems = useQuery({ ...workItemsQuery(taskId), enabled: Boolean(taskId) });
  const item = detail.data?.item;
  const instanceId = item?.current_instance?.instance_id ?? "";
  const link = useQuery({
    ...agentWorkbenchLinkQuery(taskId, workItemId, instanceId),
    enabled: Boolean(item?.stage.available && instanceId),
  });
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const enterWorkbenchRef = useRef<HTMLButtonElement>(null);
  const loadTimeoutRef = useRef<number | undefined>(undefined);
  const announceBridgeRef = useRef<() => void>(() => undefined);
  const autoLaunchAttemptRef = useRef<string | null>(null);
  const [frameKey, setFrameKey] = useState(0);
  const [frameState, setFrameState] = useState<FrameState>("loading");
  const consumeAutoLaunch = (): void => {
    const next = new URLSearchParams(searchParams);
    next.delete("start");
    setSearchParams(next, { replace: true });
  };
  const refreshWorkbench = (): void => {
    void queryClient.invalidateQueries({ queryKey: workItemDetailQuery(taskId, workItemId).queryKey });
    void queryClient.invalidateQueries({ queryKey: workItemsQuery(taskId).queryKey });
    void queryClient.invalidateQueries({ queryKey: agentWorkbenchLinkQuery(taskId, workItemId, instanceId).queryKey });
  };
  const setManualFinished = useMutation({
    mutationFn: (manualFinished: boolean) => {
      const taskRevision = link.data?.task_revision;
      if (taskRevision === undefined) throw new Error("当前任务修订尚未就绪，请重新检查工作台链接。");
      return api.setManualFinished(instanceId, manualFinished, {
        idempotency_key: operationId(manualFinished ? "finish_image" : "resume_image"),
        actor_type: "human",
        actor_id: "human_operator",
        expected_revision: taskRevision,
      });
    },
    onSuccess: refreshWorkbench,
  });
  const startPpt = useMutation({
    mutationFn: () => {
      const taskRevision = link.data?.task_revision;
      if (taskRevision === undefined) throw new Error("当前任务修订尚未就绪，请重新检查工作台链接。");
      const operation = operationId("start_ppt");
      return api.startInstance(instanceId, operation, {
        idempotency_key: operation,
        actor_type: "human",
        actor_id: "human_operator",
        expected_revision: taskRevision,
      });
    },
    onSuccess: () => {
      consumeAutoLaunch();
      refreshWorkbench();
    },
  });
  const recoverStart = useMutation({
    mutationFn: () => {
      const operation = link.data?.start_operation;
      const taskRevision = link.data?.task_revision;
      if (!operation || taskRevision === undefined) {
        throw new Error("当前没有可恢复的启动操作。");
      }
      const suffix = typeof crypto.randomUUID === "function"
        ? crypto.randomUUID().replaceAll("-", "")
        : `${Date.now()}${Math.random().toString(16).slice(2)}`;
      return api.retryStartOperation(operation.operation_id, {
        idempotency_key: `retry_start_${suffix}`.slice(0, 128),
        actor_type: "human",
        actor_id: "human_operator",
        expected_revision: taskRevision,
      });
    },
    onSuccess: () => {
      consumeAutoLaunch();
      void queryClient.invalidateQueries({ queryKey: agentWorkbenchLinkQuery(taskId, workItemId, instanceId).queryKey });
      void queryClient.invalidateQueries({ queryKey: workItemDetailQuery(taskId, workItemId).queryKey });
      void queryClient.invalidateQueries({ queryKey: workItemsQuery(taskId).queryKey });
    },
  });

  useEffect(() => {
    if (!link.data?.embeddable || !link.data.ui_url) return undefined;
    setFrameState("loading");
    loadTimeoutRef.current = window.setTimeout(() => setFrameState("failed"), 12_000);
    return () => window.clearTimeout(loadTimeoutRef.current);
  }, [frameKey, link.data?.embeddable, link.data?.ui_url]);

  useEffect(() => {
    const uiUrl = item?.agent_type === "image" && link.data?.embeddable ? link.data.ui_url : null;
    if (!uiUrl || !instanceId) return undefined;
    let targetOrigin: string;
    try {
      targetOrigin = new URL(uiUrl).origin;
    } catch {
      return undefined;
    }
    let active = true;
    let nonce = newBridgeNonce();
    const post = (payload: Record<string, unknown>): void => {
      iframeRef.current?.contentWindow?.postMessage(payload, targetOrigin);
    };
    const announce = (): void => post({
      protocol: RUNTIME_SETTINGS_BRIDGE_PROTOCOL,
      version: RUNTIME_SETTINGS_BRIDGE_VERSION,
      type: "bridge.init",
      instance_id: instanceId,
      nonce,
    });
    announceBridgeRef.current = announce;
    const onMessage = (event: MessageEvent<unknown>): void => {
      if (!active
        || event.origin !== targetOrigin
        || event.source !== iframeRef.current?.contentWindow) return;
      if (isBridgeHello(event.data, instanceId)) {
        announce();
        return;
      }
      const request = parseBridgeRequest(event.data, instanceId, nonce);
      if (!request) return;
      const consumedNonce = nonce;
      const nextNonce = newBridgeNonce();
      nonce = nextNonce;
      void executeBridgeRequest(request, instanceId, link.data?.task_revision, { taskId, workItemId }).then(
        (payload) => post({
          protocol: RUNTIME_SETTINGS_BRIDGE_PROTOCOL,
          version: RUNTIME_SETTINGS_BRIDGE_VERSION,
          type: "bridge.response",
          instance_id: instanceId,
          request_id: request.request_id,
          nonce: consumedNonce,
          next_nonce: nextNonce,
          ok: true,
          payload,
        }),
        (error: unknown) => post({
          protocol: RUNTIME_SETTINGS_BRIDGE_PROTOCOL,
          version: RUNTIME_SETTINGS_BRIDGE_VERSION,
          type: "bridge.response",
          instance_id: instanceId,
          request_id: request.request_id,
          nonce: consumedNonce,
          next_nonce: nextNonce,
          ok: false,
          error: {
            code: error instanceof ApiError ? error.code ?? "BRIDGE_REQUEST_FAILED" : "BRIDGE_REQUEST_FAILED",
            message: error instanceof Error ? error.message : "主系统未完成设置请求。",
          },
        }),
      );
    };
    window.addEventListener("message", onMessage);
    return () => {
      active = false;
      announceBridgeRef.current = () => undefined;
      window.removeEventListener("message", onMessage);
    };
  }, [instanceId, item?.agent_type, link.data?.embeddable, link.data?.task_revision, link.data?.ui_url]);

  const retry = (): void => {
    setFrameState("loading");
    setFrameKey((value) => value + 1);
    void link.refetch();
  };
  const retryAction = link.data?.link_status === "START_FAILED"
    ? () => recoverStart.mutate()
    : retry;
  const retryLabel = link.data?.link_status === "START_FAILED" ? "恢复启动" : "重新检查链接";
  const unfinishedImages = (workItems.data?.items ?? [])
    .filter((workItem) => workItem.agent_type === "image" && !workItem.current_instance?.manual_finished)
    .map((workItem) => workItem.current_instance?.instance_id)
    .filter((value): value is string => Boolean(value));
  const taskConfirmed = detail.data?.task.status === "RUNNING" || detail.data?.task.status === "FAILED";
  const pptReady = item?.agent_type === "ppt" && item.current_instance?.status === "READY";
  const canStartPpt = Boolean(pptReady && taskConfirmed && workItems.isSuccess && unfinishedImages.length === 0);
  const autoLaunchAction = item?.agent_type === "ppt"
    ? pptAutoLaunchAction({
        requested: autoLaunchRequested,
        canStart: canStartPpt,
        linkStatus: link.data?.link_status,
        retryAllowed: Boolean(link.data?.start_operation?.retry_allowed),
      })
    : null;
  useEffect(() => {
    if (!autoLaunchAction || !instanceId) return;
    const attemptKey = `${instanceId}:${autoLaunchAction}:${link.data?.start_operation?.operation_id ?? "new"}`;
    if (autoLaunchAttemptRef.current === attemptKey) return;
    autoLaunchAttemptRef.current = attemptKey;
    if (autoLaunchAction === "recover") recoverStart.mutate();
    else startPpt.mutate();
  }, [autoLaunchAction, instanceId, link.data?.start_operation?.operation_id]);

  if (detail.isPending) {
    return <section className={`workbench-page agent-workbench-state${focusMode ? " agent-workbench--focus" : ""}`} role="status">正在读取专业 WorkItem…</section>;
  }
  if (detail.isError || !item) {
    return (
      <section className={`workbench-page agent-workbench-state${focusMode ? " agent-workbench--focus" : ""}`} role="alert">
        <strong>无法读取逻辑子任务</strong>
        <p>{detail.error?.message}</p>
        <button className="workbench-secondary-button" type="button" onClick={() => void detail.refetch()}>重新读取</button>
      </section>
    );
  }

  const agentLabel = item.agent_type === "image" ? "Image Agent" : "PPT Agent";
  if (!item.stage.available) {
    return (
      <section className={`workbench-page agent-workbench${focusMode ? " agent-workbench--focus" : ""}`} aria-labelledby="agent-workbench-title">
        <header className="agent-workbench__context">
          <div><p className="workbench-eyebrow">阶段 {item.stage.position}</p><h1 id="agent-workbench-title">{item.title}</h1><p>当前任务需要的 {agentLabel} 创作能力尚未开放，任务不会被误标为完成。</p></div>
          <Link className="workbench-secondary-button" to={`/tasks/${encodeURIComponent(taskId)}/board`}>返回看板</Link>
        </header>
        {focusMode ? null : <TaskTabs taskId={taskId} />}
        <div className="agent-workbench-boundary" role="note"><Icon name="status" /><div><h2>{agentLabel} 工作台暂不可用</h2><p>请返回看板调整计划，或选择当前已开放的创作能力。</p></div></div>
      </section>
    );
  }

  const readyLink = link.data?.embeddable && link.data.ui_url ? link.data : undefined;
  const imageManuallyFinished = Boolean(item.current_instance?.manual_finished);
  const gateDetails = startPpt.error instanceof ApiError
    && Array.isArray(startPpt.error.details?.unfinished_instance_ids)
      ? startPpt.error.details.unfinished_instance_ids.filter((value): value is string => typeof value === "string")
      : unfinishedImages;
  const showPptStart = pptReady && (!link.data || link.data.link_status === "NO_UI_URL");
  const pptGateMessage = !taskConfirmed
    ? "请先确认任务启动。"
    : workItems.isPending
      ? "正在检查 Image 实例状态…"
      : workItems.isError
        ? "无法检查 PPT 启动门禁，请重新读取后再启动。"
        : gateDetails.length
          ? `仍有 ${gateDetails.length} 个 Image 实例未标记人工结束。`
          : "门禁已满足，可以启动 PPT Agent。";
  const failureMessage = frameState === "failed"
    ? "工作台在 12 秒内未完成加载。可重新检查、返回看板，或在新标签页专注模式中尝试。"
    : link.data?.link_status === "FRAME_BLOCKED"
      ? "当前工作台拒绝安全内嵌。可直接打开原始工作台，或返回看板。"
      : link.data?.link_status === "NO_UI_URL"
        ? "工作台仍在准备中，请稍后重新检查。"
        : link.data?.link_status === "STARTING"
          ? `启动请求已保存，系统正在准备制品并启动 ${agentLabel}。`
          : link.data?.link_status === "START_FAILED"
            ? link.data.start_operation?.last_error?.message ?? `${agentLabel} 启动失败，可安全恢复启动。`
        : link.data?.link_status === "ADAPTER_UNAVAILABLE"
          ? link.data.diagnostic ?? "专业工作台暂时不可用，请检查运行环境；当前任务进度已保留。"
          : link.error?.message ?? "当前专业工作台暂时不可用，请稍后重新检查。";

  const focusPath = `/tasks/${encodeURIComponent(taskId)}/work-items/${encodeURIComponent(workItemId)}/focus`;
  return (
    <section className={`workbench-page agent-workbench${focusMode ? " agent-workbench--focus" : ""}`} aria-label={item.title}>
      {focusMode ? null : (
        <TaskTabs
          taskId={taskId}
          trailing={link.data?.ui_url ? (
            <a className="workbench-task-tabs__focus" href={focusPath} target="_blank" rel="noopener noreferrer"><Icon name="external-link" />新标签页</a>
          ) : null}
        />
      )}
      {item.agent_type === "image" && item.current_instance ? (
        <div className="agent-workbench-gate" aria-live="polite">
          <div>
            <strong>{imageManuallyFinished ? "已允许 PPT 阶段启动" : "PPT 阶段仍在等待"}</strong>
            <span>此标记只控制 PPT 启动门禁，不会停止 Image Agent 或改变交付状态。</span>
          </div>
          <button
            className="workbench-secondary-button"
            type="button"
            disabled={setManualFinished.isPending || link.data?.task_revision === undefined}
            onClick={() => setManualFinished.mutate(!imageManuallyFinished)}
          >
            {setManualFinished.isPending ? "正在保存…" : imageManuallyFinished ? "改回进行中" : "标记人工结束"}
          </button>
          {setManualFinished.isError ? <p className="workbench-inline-error" role="alert">{setManualFinished.error.message}</p> : null}
        </div>
      ) : null}
      {showPptStart ? (
        <div className="agent-workbench-gate" aria-live="polite">
          <div>
            <strong>PPT 工作台待启动</strong>
            <span>{pptGateMessage}</span>
            {workItems.isSuccess && gateDetails.length ? <code>{gateDetails.join("、")}</code> : null}
            {taskConfirmed && workItems.isError ? (
              <>
                <p className="workbench-inline-error" role="alert">{workItems.error.message}</p>
                <button className="workbench-secondary-button" type="button" onClick={() => void workItems.refetch()}>重新检查门禁</button>
              </>
            ) : null}
          </div>
          <button className="workbench-primary-button" type="button" disabled={!canStartPpt || startPpt.isPending} onClick={() => startPpt.mutate()}>
            {startPpt.isPending ? "正在启动…" : "启动 PPT 工作台"}
          </button>
          {startPpt.isError ? <p className="workbench-inline-error" role="alert">{startPpt.error.message}</p> : null}
        </div>
      ) : null}
      {link.isPending ? <div className="agent-workbench__loading" role="status">正在获取受控 {agentLabel} 工作台地址…</div> : null}
      {link.isError || (link.data && !readyLink && !showPptStart) ? (
        <WorkbenchFailure link={link.data} message={failureMessage} onRetry={retryAction} retryLabel={retryLabel} retryPending={recoverStart.isPending} taskId={taskId} workItemId={workItemId} focusMode={focusMode} agentType={item.agent_type} />
      ) : null}
      {readyLink && frameState === "failed" ? (
        <WorkbenchFailure link={readyLink} message={failureMessage} onRetry={retry} retryLabel="重新检查链接" retryPending={false} taskId={taskId} workItemId={workItemId} focusMode={focusMode} agentType={item.agent_type} />
      ) : null}
      {readyLink && frameState !== "failed" ? (
        <>
          <div className="agent-workbench__actions">
            <button ref={enterWorkbenchRef} className="workbench-secondary-button" type="button" onClick={() => iframeRef.current?.focus()}>
              跳到 {agentLabel} 工作台
            </button>
            <span role="status" aria-live="polite">{frameState === "ready" ? `${agentLabel} 已连接` : `${agentLabel} 正在连接`}</span>
          </div>
          <div className="agent-workbench-frame">
            {frameState === "loading" ? <div className="agent-workbench-frame__overlay" role="status">{agentLabel} 工作台正在加载…</div> : null}
            <iframe
              key={`${readyLink.instance_id}-${frameKey}`}
              ref={iframeRef}
              className="agent-workbench-frame__iframe"
              src={readyLink.ui_url ?? undefined}
              title={`${agentLabel} 工作台：${item.title}`}
              sandbox="allow-downloads allow-forms allow-modals allow-popups allow-popups-to-escape-sandbox allow-same-origin allow-scripts"
              allow="clipboard-read; clipboard-write"
              referrerPolicy="origin"
              onLoad={() => {
                window.clearTimeout(loadTimeoutRef.current);
                setFrameState("ready");
                announceBridgeRef.current();
              }}
              onError={() => { window.clearTimeout(loadTimeoutRef.current); setFrameState("failed"); }}
            />
            <div className="agent-workbench-frame__footer">
              <button className="workbench-secondary-button" type="button" onClick={() => enterWorkbenchRef.current?.focus()}>返回工作台操作栏</button>
            </div>
          </div>
        </>
      ) : null}
    </section>
  );
}
