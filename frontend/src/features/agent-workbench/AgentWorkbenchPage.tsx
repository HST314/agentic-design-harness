import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  agentWorkbenchLinkQuery,
  api,
  inboxQuery,
  workItemDetailQuery,
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

export function workbenchFocusPath(taskId: string, workItemId: string): string {
  return `/tasks/${encodeURIComponent(taskId)}/work-items/${encodeURIComponent(workItemId)}/focus`;
}

export function FocusTabLink({ taskId, workItemId }: {
  taskId: string;
  workItemId: string;
}): React.JSX.Element {
  return (
    <a
      className="workbench-task-tabs__focus"
      href={workbenchFocusPath(taskId, workItemId)}
      target="_blank"
      rel="noopener noreferrer"
      aria-label="在新标签页打开全屏工作台"
    >
      <Icon name="external-link" />新标签页
    </a>
  );
}

export async function executeBridgeRequest(
  request: RuntimeSettingsBridgeRequest,
  instanceId: string,
  taskRevision: number | undefined,
  scope: { taskId: string; workItemId: string },
): Promise<unknown> {
  if (request.action === "runtime_settings.get") {
    return api.instanceRuntimeSettings(instanceId);
  }
  if (request.action === "delivery.status") {
    const bundleId = String(request.payload.bundle_id);
    const bundles = await api.deliveryBundles(scope.taskId);
    const candidate = bundles.candidates.find((item) => (
      item.bundle_id === bundleId && item.instance_id === instanceId
    ));
    return { bundle_id: bundleId, status: candidate?.status ?? "UNKNOWN" };
  }
  if (request.action === "delivery.complete") {
    const bundleId = String(request.payload.bundle_id);
    // Poll on a fixed deadline so the loop can never outrun the child's
    // 30 second bridge timeout, no matter how slow a single read gets.
    // Every read is bound to the same deadline via AbortController: a slow
    // in-flight GET started just before the deadline is aborted instead of
    // hanging past the bridge timeout.
    const deadline = Date.now() + 25_000;
    while (Date.now() < deadline) {
      const controller = new AbortController();
      const abortAt = setTimeout(() => controller.abort(), deadline - Date.now());
      let bundles: Awaited<ReturnType<typeof api.deliveryBundles>>;
      try {
        bundles = await api.deliveryBundles(scope.taskId, controller.signal);
      } catch (error) {
        if (controller.signal.aborted) break;
        throw error;
      } finally {
        clearTimeout(abortAt);
      }
      const candidate = bundles.candidates.find((item) => (
        item.bundle_id === bundleId && item.instance_id === instanceId
      ));
      if (candidate?.status === "PUBLISHED") {
        return { bundle_id: bundleId, status: "PUBLISHED" };
      }
      if (candidate && candidate.status !== "PENDING_CONFIRMATION") {
        throw new Error("当前交付候选已经处理，无法再次完成入库。");
      }
      const review = bundles.reviews.find((item) => (
        item.bundle_id === bundleId && item.approval.status === "PENDING"
      ));
      if (candidate && review) {
        const operationId = bridgeIdempotencyKey(request.action, request.request_id);
        return api.resolveApproval(review.approval.approval_id, {
          decision: "APPROVED",
          action: "publish_bundle",
          payload: {},
          operation_id: operationId,
          envelope: {
            idempotency_key: operationId,
            actor_type: "human",
            actor_id: "human_operator",
            expected_revision: review.approval_revision,
          },
        });
      }
      await new Promise((resolve) => window.setTimeout(resolve, 250));
    }
    throw new Error("主系统尚未完成交付候选对账，请稍后再点击完成。");
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
  agentType: "general" | "image" | "ppt";
}): React.JSX.Element {
  const agentLabel = agentType === "general" ? "通用助手" : agentType === "image" ? "图片助手" : "演示文稿助手";
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
              href={workbenchFocusPath(taskId, workItemId)}
              target="_blank"
              rel="noopener noreferrer"
            >
              <Icon name="external-link" />在新标签页中尝试
            </a>
          ) : null}
          <Link className="workbench-secondary-button" to={`/tasks/${encodeURIComponent(taskId)}/${agentType === "ppt" ? "master" : "board"}`}>
            {agentType === "ppt" ? "返回 Master" : "返回看板"}
          </Link>
        </div>
      </div>
    </div>
  );
}

export function AgentWorkbenchPage({ focusMode = false }: { focusMode?: boolean }): React.JSX.Element {
  const { taskId = "", workItemId = "" } = useParams();
  const queryClient = useQueryClient();
  const detail = useQuery(workItemDetailQuery(taskId, workItemId));
  const item = detail.data?.item;
  const instanceId = item?.current_instance?.instance_id ?? "";
  const link = useQuery({
    ...agentWorkbenchLinkQuery(taskId, workItemId, instanceId),
    enabled: Boolean(item?.stage.available && instanceId),
  });
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const loadTimeoutRef = useRef<number | undefined>(undefined);
  const announceBridgeRef = useRef<() => void>(() => undefined);
  const [frameKey, setFrameKey] = useState(0);
  const [frameState, setFrameState] = useState<FrameState>("loading");
  const viewedInstanceRef = useRef<string | null>(null);

  useEffect(() => {
    if (!instanceId || viewedInstanceRef.current === instanceId) return;
    viewedInstanceRef.current = instanceId;
    void api.viewInstance(instanceId).then(() => {
      void queryClient.invalidateQueries({ queryKey: inboxQuery.queryKey });
    }).catch(() => undefined);
  }, [instanceId, queryClient]);

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

  if (detail.isPending) {
    return <section className={`workbench-page agent-workbench-state${focusMode ? " agent-workbench--focus" : ""}`} role="status">正在读取专业子任务…</section>;
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

  const agentLabel = item.agent_type === "general" ? "通用助手" : item.agent_type === "image" ? "图片助手" : "演示文稿助手";
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
  const failureMessage = frameState === "failed"
    ? "工作台在 12 秒内未完成加载。可重新检查、返回看板，或在新标签页专注模式中尝试。"
    : link.data?.link_status === "FRAME_BLOCKED"
      ? "当前工作台拒绝安全内嵌。可直接打开原始工作台，或返回看板。"
      : link.data?.link_status === "NO_UI_URL"
        ? item.agent_type === "ppt"
          ? "PPT 工作台尚未启动，请返回 Master 面板启动。"
          : "工作台仍在准备中，请稍后重新检查。"
        : link.data?.link_status === "STARTING"
          ? `启动请求已保存，系统正在准备制品并启动 ${agentLabel}。`
          : link.data?.link_status === "START_FAILED"
            ? link.data.start_operation?.last_error?.message ?? `${agentLabel} 启动失败，可安全恢复启动。`
        : link.data?.link_status === "ADAPTER_UNAVAILABLE"
          ? link.data.diagnostic ?? "专业工作台暂时不可用，请检查运行环境；当前任务进度已保留。"
          : link.error?.message ?? "当前专业工作台暂时不可用，请稍后重新检查。";

  return (
    <section className={`workbench-page agent-workbench${focusMode ? " agent-workbench--focus" : ""}`} aria-label={item.title}>
      {focusMode ? null : (
        <TaskTabs
          taskId={taskId}
          trailing={link.data?.ui_url ? <FocusTabLink taskId={taskId} workItemId={workItemId} /> : null}
        />
      )}
      {link.isPending ? <div className="agent-workbench__loading" role="status">正在获取受控 {agentLabel} 工作台地址…</div> : null}
      {link.isError || (link.data && !readyLink) ? (
        <WorkbenchFailure link={link.data} message={failureMessage} onRetry={retry} retryLabel="重新检查链接" retryPending={false} taskId={taskId} workItemId={workItemId} focusMode={focusMode} agentType={item.agent_type} />
      ) : null}
      {readyLink && frameState === "failed" ? (
        <WorkbenchFailure link={readyLink} message={failureMessage} onRetry={retry} retryLabel="重新检查链接" retryPending={false} taskId={taskId} workItemId={workItemId} focusMode={focusMode} agentType={item.agent_type} />
      ) : null}
      {readyLink && frameState !== "failed" ? (
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
          </div>
      ) : null}
    </section>
  );
}
