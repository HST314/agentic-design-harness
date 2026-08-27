import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import type {
  HarnessSettingsDocument,
  ImageAgentSettingsDocument,
  SystemSettingsPreview,
} from "../../api/client";
import { ApiError } from "../../api/client";
import { api, systemSettingsQuery, taskHistoryQuery } from "../../api/queries";

function broadcastIdempotencyKey(): string {
  const suffix = typeof crypto.randomUUID === "function"
    ? crypto.randomUUID().replaceAll("-", "")
    : `${Date.now()}${Math.random().toString(16).slice(2)}`;
  return `task_settings_broadcast_${suffix}`.slice(0, 128);
}

type SettingsTab = "harness" | "image-agent";
type Draft = {
  harness: HarnessSettingsDocument;
  image: ImageAgentSettingsDocument;
};

const modelStates: Array<{
  key: string;
  label: string;
  group: "text_models" | "vlm_models" | "image_models";
}> = [
  { key: "intake_clarify", label: "需求澄清", group: "text_models" },
  { key: "confirmation_build", label: "任务书生成", group: "text_models" },
  { key: "initial_candidate_generation", label: "初始候选出图", group: "image_models" },
  { key: "self_check_inspection", label: "质量检查", group: "vlm_models" },
  { key: "self_check_rework", label: "质量返修", group: "image_models" },
  { key: "human_prompt_rework", label: "人工提示返修", group: "image_models" },
];

function clone<T>(value: T): T {
  return JSON.parse(JSON.stringify(value)) as T;
}

function valueAt(value: unknown, path: string): unknown {
  return path.split(".").reduce<unknown>((current, key) => (
    current && typeof current === "object"
      ? (current as Record<string, unknown>)[key]
      : undefined
  ), value);
}

function assignAt(value: Record<string, unknown>, path: string, next: unknown): void {
  const parts = path.split(".");
  let current = value;
  parts.slice(0, -1).forEach((key) => {
    current = current[key] as Record<string, unknown>;
  });
  current[parts.at(-1) ?? ""] = next;
}

function mergeDirty(latest: Draft, draft: Draft, paths: Set<string>): Draft {
  const merged = clone(latest) as unknown as Record<string, unknown>;
  const source = draft as unknown as Record<string, unknown>;
  paths.forEach((path) => assignAt(merged, path, clone(valueAt(source, path))));
  return merged as unknown as Draft;
}

function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "继承默认";
  if (typeof value === "boolean") return value ? "开启" : "关闭";
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value);
}

function NumberField({
  id,
  label,
  value,
  min,
  max,
  helper,
  onChange,
}: {
  id: string;
  label: string;
  value: number;
  min: number;
  max: number;
  helper?: string;
  onChange: (value: number) => void;
}): React.JSX.Element {
  return (
    <label className="settings-field" htmlFor={id}>
      <span>{label}</span>
      <input
        id={id}
        type="number"
        min={min}
        max={max}
        value={value}
        onChange={(event) => onChange(event.currentTarget.valueAsNumber)}
      />
      {helper ? <small>{helper}</small> : null}
    </label>
  );
}

function ToggleField({
  id,
  label,
  checked,
  helper,
  onChange,
}: {
  id: string;
  label: string;
  checked: boolean;
  helper?: string;
  onChange: (value: boolean) => void;
}): React.JSX.Element {
  return (
    <label className="settings-toggle" htmlFor={id}>
      <span><strong>{label}</strong>{helper ? <small>{helper}</small> : null}</span>
      <input
        id={id}
        type="checkbox"
        checked={checked}
        onChange={(event) => onChange(event.currentTarget.checked)}
      />
    </label>
  );
}

export function SystemSettingsPage(): React.JSX.Element {
  const queryClient = useQueryClient();
  const settings = useQuery(systemSettingsQuery);
  const tasks = useQuery(taskHistoryQuery);
  const [tab, setTab] = useState<SettingsTab>("harness");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [baseRevision, setBaseRevision] = useState("");
  const [preview, setPreview] = useState<SystemSettingsPreview | null>(null);
  const [publicationMessage, setPublicationMessage] = useState<string | null>(null);
  const [broadcastTaskId, setBroadcastTaskId] = useState("");
  const [broadcastMessage, setBroadcastMessage] = useState<string | null>(null);
  const dirtyPaths = useRef(new Set<string>());
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);

  const taskItems = tasks.data?.items ?? [];
  useEffect(() => {
    const first = taskItems[0];
    if (!broadcastTaskId && first) {
      setBroadcastTaskId(first.task_id);
    }
  }, [broadcastTaskId, taskItems]);

  const broadcastMutation = useMutation({
    mutationFn: () => {
      const task = taskItems.find((item) => item.task_id === broadcastTaskId);
      if (!task) throw new Error("请选择要下发的任务。");
      return api.broadcastTaskSettings(task.task_id, {
        idempotency_key: broadcastIdempotencyKey(),
        actor_type: "human",
        actor_id: "human_operator",
        expected_revision: task.revision,
      });
    },
    onSuccess: (result) => {
      setBroadcastMessage(
        result.failed
          ? `下发完成，但 ${result.failed} 个实例失败（${result.items.find((item) => item.status === "FAILED")?.message ?? "未知错误"}）。`
          : `已下发：${result.updated} 个实例已更新，${result.waiting_safe_point} 个将在安全检查点应用，${result.unchanged} 个已是最新。`,
      );
    },
  });

  useEffect(() => {
    if (!settings.data || dirtyPaths.current.size > 0) return;
    setDraft({
      harness: clone(settings.data.harness_settings),
      image: clone(settings.data.image_agent_settings),
    });
    setBaseRevision(settings.data.revision);
  }, [settings.data]);

  const update = (path: string, value: unknown): void => {
    setDraft((current) => {
      if (!current) return current;
      const next = clone(current) as unknown as Record<string, unknown>;
      assignAt(next, path, value);
      return next as unknown as Draft;
    });
    dirtyPaths.current.add(path);
    setPreview(null);
    setPublicationMessage(null);
  };

  const previewMutation = useMutation({
    mutationFn: () => {
      if (!draft) throw new Error("设置尚未载入。");
      return api.previewSystemSettings({
        base_revision: baseRevision,
        harness_settings: draft.harness,
        image_agent_settings: draft.image,
      });
    },
    onSuccess: setPreview,
  });

  const publishMutation = useMutation({
    mutationFn: () => {
      if (!preview) throw new Error("请先预览本次更改。");
      return api.publishSystemSettings({
        preview_id: preview.preview_id,
        base_revision: preview.base_revision,
        harness_settings: preview.harness_settings,
        image_agent_settings: preview.image_agent_settings,
        actor_id: "human_operator",
      });
    },
    onSuccess: async (result) => {
      const waiting = result.distribution.waiting_safe_point;
      const failed = result.distribution.failed;
      setPublicationMessage(
        failed
          ? `全局设置已发布，${failed} 个同步目标需要处理。`
          : waiting
            ? `全局设置已发布，${waiting} 个运行中实例将在最近安全检查点应用。`
            : "全局设置已发布并完成同步。",
      );
      dirtyPaths.current.clear();
      setPreview(null);
      await queryClient.invalidateQueries({ queryKey: systemSettingsQuery.queryKey });
    },
  });

  const conflict = [previewMutation.error, publishMutation.error].find(
    (error) => error instanceof ApiError && error.code === "SETTINGS_REVISION_CONFLICT",
  );

  const recoverConflict = async (): Promise<void> => {
    if (!draft) return;
    const latest = await api.systemSettings();
    const merged = mergeDirty(
      { harness: latest.harness_settings, image: latest.image_agent_settings },
      draft,
      dirtyPaths.current,
    );
    setDraft(merged);
    setBaseRevision(latest.revision);
    setPreview(null);
    previewMutation.reset();
    publishMutation.reset();
    queryClient.setQueryData(systemSettingsQuery.queryKey, latest);
  };

  const onTabKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>): void => {
    if (!(["ArrowLeft", "ArrowRight"] as string[]).includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "ArrowRight" ? (tab === "harness" ? 1 : 0) : (tab === "harness" ? 1 : 0);
    const nextTab: SettingsTab = next === 0 ? "harness" : "image-agent";
    setTab(nextTab);
    tabRefs.current[next]?.focus();
  };

  if (settings.isError) {
    return <div className="workbench-page"><p className="workbench-inline-error" role="alert">{settings.error.message}</p></div>;
  }
  if (settings.isPending || !draft) {
    return <div className="workbench-page"><p className="settings-loading" role="status">正在载入全局设置…</p></div>;
  }

  const options = settings.data.model_options;
  const busy = previewMutation.isPending || publishMutation.isPending;

  return (
    <div className="workbench-page settings-page">
      <header className="workbench-page__header settings-page__header">
        <div>
          <p className="workbench-eyebrow">全局控制面</p>
          <h1>全局设置</h1>
          <p>发布后，新任务立即继承；未启动实例立即同步；运行中 Image Agent 在最近安全检查点自动建立配置分支。</p>
        </div>
        <span className="settings-revision" title={baseRevision}>修订 {baseRevision.slice(-8)}</span>
      </header>

      <div className="settings-tabs" role="tablist" aria-label="全局设置范围">
        {([
          ["harness", "Harness 设置", "运行映射与控制策略"],
          ["image-agent", "子 Agent 设置", "Image Agent 全局默认"],
        ] as const).map(([key, label, description], index) => (
          <button
            key={key}
            ref={(node) => { tabRefs.current[index] = node; }}
            id={`settings-tab-${key}`}
            type="button"
            role="tab"
            aria-selected={tab === key}
            aria-controls={`settings-panel-${key}`}
            tabIndex={tab === key ? 0 : -1}
            onClick={() => setTab(key)}
            onKeyDown={onTabKeyDown}
          >
            <strong>{label}</strong><small>{description}</small>
          </button>
        ))}
      </div>

      {tab === "harness" ? (
        <div id="settings-panel-harness" role="tabpanel" aria-labelledby="settings-tab-harness" className="settings-panel">
          <section className="settings-section">
            <div className="settings-section__heading"><h2>模型映射</h2><p>选择 Harness 和 Image Agent 各类调用的批准模型。</p></div>
            <div className="settings-grid settings-grid--two">
              {([
                ["master", "Master 编排模型", "text_models"],
                ["text_reasoning", "文本推理模型", "text_models"],
                ["vision_understanding", "视觉理解模型", "vlm_models"],
                ["image_generation", "图像生成模型", "image_models"],
              ] as const).map(([key, label, group]) => (
                <label className="settings-field" key={key} htmlFor={`model-${key}`}>
                  <span>{label}</span>
                  <select id={`model-${key}`} value={draft.harness.models[key]} onChange={(event) => update(`harness.models.${key}`, event.currentTarget.value)}>
                    {(options[group] ?? []).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
                  </select>
                </label>
              ))}
            </div>
          </section>

          <section className="settings-section">
            <div className="settings-section__heading"><h2>Master 与文档处理</h2><p>这些值在发布后的新任务和未启动任务上生效。</p></div>
            <div className="settings-grid settings-grid--three">
              <NumberField id="master-timeout" label="模型超时（秒）" min={1} max={3600} value={draft.harness.master.model_timeout_seconds} onChange={(value) => update("harness.master.model_timeout_seconds", value)} />
              <NumberField id="master-rounds" label="最大工具轮数" min={1} max={100} value={draft.harness.master.max_tool_rounds} onChange={(value) => update("harness.master.max_tool_rounds", value)} />
              <NumberField id="master-questions" label="最大澄清问题数" min={0} max={20} value={draft.harness.master.max_clarification_questions} onChange={(value) => update("harness.master.max_clarification_questions", value)} />
              <NumberField id="document-files" label="单任务最大文件数" min={1} max={1000} value={draft.harness.document_processing.max_files_per_task} onChange={(value) => update("harness.document_processing.max_files_per_task", value)} />
              <NumberField id="document-pages" label="PDF 最大页数" min={1} max={10000} value={draft.harness.document_processing.max_pdf_pages} onChange={(value) => update("harness.document_processing.max_pdf_pages", value)} />
              <label className="settings-field" htmlFor="visual-analysis"><span>视觉分析策略</span><select id="visual-analysis" value={draft.harness.document_processing.visual_analysis} onChange={(event) => update("harness.document_processing.visual_analysis", event.currentTarget.value)}><option value="auto">自动</option><option value="always">始终</option><option value="never">从不</option></select></label>
            </div>
            <div className="settings-toggle-list">
              <ToggleField id="plan-confirmation" label="计划启动前必须确认" checked={draft.harness.master.require_plan_confirmation} onChange={(value) => update("harness.master.require_plan_confirmation", value)} />
              <ToggleField id="source-citations" label="要求来源引用" checked={draft.harness.document_processing.require_source_citations} onChange={(value) => update("harness.document_processing.require_source_citations", value)} />
            </div>
          </section>

          <section className="settings-section settings-section--muted">
            <div className="settings-section__heading"><h2>进程边界</h2><p>监听地址与 Supervisor 端口保留在 `config/runtime.yaml`，当前进程不会热切换这些基础设施值。</p></div>
            <dl className="settings-summary"><div><dt>Harness</dt><dd>{draft.harness.server.host}:{draft.harness.server.port}</dd></div><div><dt>Supervisor</dt><dd>{draft.harness.supervisor.port_range_start}–{draft.harness.supervisor.port_range_end}</dd></div><div><dt>日志级别</dt><dd>{draft.harness.server.log_level}</dd></div></dl>
          </section>
        </div>
      ) : (
        <div id="settings-panel-image-agent" role="tabpanel" aria-labelledby="settings-tab-image-agent" className="settings-panel">
          <section className="settings-section">
            <div className="settings-section__heading"><h2>提问与数据库</h2><p>数据库默认不使用；修改会分发到既有可执行实例。</p></div>
            <div className="settings-grid settings-grid--three">
              <label className="settings-field" htmlFor="question-preference"><span>提问偏好</span><select id="question-preference" value={draft.image.question_preference === "on_demand" ? "blocking_only" : draft.image.question_preference} onChange={(event) => update("image.question_preference", event.currentTarget.value)}><option value="proactive">主动澄清</option><option value="blocking_only">仅阻塞时提问</option></select></label>
              <NumberField id="auto-questions" label="自动问题上限" min={0} max={10} value={draft.image.max_auto_questions} onChange={(value) => update("image.max_auto_questions", value)} />
              <NumberField id="question-budget" label="澄清总预算" min={0} max={100} value={draft.image.clarification_total_budget} onChange={(value) => update("image.clarification_total_budget", value)} />
              {([
                ["category_constraint", "品类约束库"],
                ["style_direction", "风格方向库"],
              ] as const).map(([key, label]) => (
                <label className="settings-field" key={key} htmlFor={`library-${key}`}><span>{label}</span><select id={`library-${key}`} value={draft.image[key].release} onChange={(event) => update(`image.${key}.release`, event.currentTarget.value)}><option value="off">不使用数据库</option><option value="manual">人工选择版本</option><option value="auto">自动选择版本</option></select></label>
              ))}
            </div>
          </section>

          <section className="settings-section">
            <div className="settings-section__heading"><h2>候选与出图</h2><p>控制并发、输出规格和交付格式。</p></div>
            <div className="settings-grid settings-grid--three">
              <NumberField id="candidate-concurrency" label="候选并发数" min={1} max={5} value={draft.image.candidate_concurrency} onChange={(value) => update("image.candidate_concurrency", value)} />
              <label className="settings-field" htmlFor="output-size"><span>默认输出尺寸</span><input id="output-size" value={draft.image.default_output_size} pattern="(?:[1-9][0-9]{1,4}x[1-9][0-9]{1,4}|[124]K)" onChange={(event) => update("image.default_output_size", event.currentTarget.value)} /></label>
              <label className="settings-field" htmlFor="response-format"><span>响应格式</span><select id="response-format" value={draft.image.response_format} onChange={(event) => update("image.response_format", event.currentTarget.value)}><option value="url">URL</option><option value="b64_json">Base64 JSON</option></select></label>
            </div>
            <div className="settings-toggle-list"><ToggleField id="watermark" label="添加水印" checked={draft.image.watermark} onChange={(value) => update("image.watermark", value)} /></div>
          </section>

          <section className="settings-section">
            <div className="settings-section__heading"><h2>质量自检</h2><p>控制修复方式、轮数和提前结束条件。</p></div>
            <div className="settings-grid settings-grid--three">
              <label className="settings-field" htmlFor="self-check-mode"><span>终止模式</span><select id="self-check-mode" value={draft.image.self_check.termination} onChange={(event) => update("image.self_check.termination", event.currentTarget.value)}><option value="solo">独立检查</option><option value="fix">检查并修复</option></select></label>
              <NumberField id="fixed-rounds" label="固定轮数" min={1} max={20} value={draft.image.self_check.fixed_rounds} onChange={(value) => update("image.self_check.fixed_rounds", value)} />
              <NumberField id="max-rounds" label="最大轮数" min={1} max={50} value={draft.image.self_check.max_rounds} onChange={(value) => update("image.self_check.max_rounds", value)} />
            </div>
            <div className="settings-toggle-list"><ToggleField id="stop-on-pass" label="通过后提前结束" checked={draft.image.self_check.stop_early_on_pass} onChange={(value) => update("image.self_check.stop_early_on_pass", value)} /></div>
          </section>

          <section className="settings-section">
            <div className="settings-section__heading"><h2>高级模型覆盖</h2><p>留空时继承 Harness 模型映射。</p></div>
            <div className="settings-grid settings-grid--two">
              {modelStates.map(({ key, label, group }) => (
                <label className="settings-field" key={key} htmlFor={`agent-model-${key}`}><span>{label}</span><select id={`agent-model-${key}`} value={draft.image.advanced_model_overrides[key] ?? ""} onChange={(event) => update(`image.advanced_model_overrides.${key}`, event.currentTarget.value || null)}><option value="">继承 Harness 映射</option>{(options[group] ?? []).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>
              ))}
            </div>
          </section>

          <section className="settings-section">
            <div className="settings-section__heading"><h2>按任务下发</h2><p>把当前已发布的全局 Image Agent 默认立即推送到所选任务的全部 Image 实例；未启动实例立即生效，运行中实例在最近安全检查点应用，实例私有覆盖保持不变。</p></div>
            <div className="settings-grid settings-grid--two">
              <label className="settings-field" htmlFor="broadcast-task">
                <span>目标任务</span>
                <select id="broadcast-task" value={broadcastTaskId} onChange={(event) => { setBroadcastTaskId(event.currentTarget.value); setBroadcastMessage(null); broadcastMutation.reset(); }}>
                  {taskItems.length ? taskItems.map((item) => (
                    <option key={item.task_id} value={item.task_id}>{item.title}</option>
                  )) : <option value="">暂无可选任务</option>}
                </select>
              </label>
            </div>
            <div className="settings-toggle-list">
              <button
                type="button"
                className="workbench-secondary-button"
                disabled={broadcastMutation.isPending || !broadcastTaskId}
                onClick={() => broadcastMutation.mutate()}
              >
                {broadcastMutation.isPending ? "正在下发…" : "立即下发给本任务所有 image-agent"}
              </button>
            </div>
            {broadcastMessage ? <p role="status">{broadcastMessage}</p> : null}
            {broadcastMutation.isError ? <p className="workbench-inline-error" role="alert">{broadcastMutation.error.message}</p> : null}
          </section>
        </div>
      )}

      <aside className="settings-publish" aria-label="设置发布">
        <div>
          <strong>{preview ? `已校验 ${preview.changes.length} 项更改` : dirtyPaths.current.size ? "有尚未校验的更改" : "当前设置已载入"}</strong>
          <p aria-live="polite">{publishMutation.isPending ? "正在发布并分发…" : previewMutation.isPending ? "正在校验配置…" : publicationMessage ?? "先预览差异，再发布到全局。"}</p>
        </div>
        <div className="settings-publish__actions">
          <button type="button" className="workbench-secondary-button" disabled={busy || dirtyPaths.current.size === 0} onClick={() => previewMutation.mutate()}>{previewMutation.isPending ? "校验中…" : "预览更改"}</button>
          <button type="button" className="workbench-primary-button" disabled={busy || !preview || preview.changes.length === 0} onClick={() => publishMutation.mutate()}>{publishMutation.isPending ? "发布中…" : "发布并同步"}</button>
        </div>
      </aside>

      {preview ? <section className="settings-diff" aria-labelledby="settings-diff-title"><div className="settings-section__heading"><h2 id="settings-diff-title">发布差异</h2><p>完成历史保持不变；运行中实例从安全检查点创建配置分支。</p></div>{preview.changes.length ? <ul>{preview.changes.map((change) => <li key={change.field}><code>{change.field}</code><span>{displayValue(change.before)}</span><span aria-hidden="true">→</span><strong>{displayValue(change.after)}</strong></li>)}</ul> : <p>没有实际更改。</p>}</section> : null}

      {previewMutation.isError || publishMutation.isError ? <div className="settings-error" role="alert"><strong>设置操作未完成</strong><p>{(publishMutation.error ?? previewMutation.error)?.message}</p>{conflict ? <button type="button" className="workbench-secondary-button" onClick={() => void recoverConflict()}>载入最新修订并保留未保存修改</button> : null}</div> : null}
    </div>
  );
}
