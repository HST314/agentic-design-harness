import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import type {
  CredentialSummary,
  GlobalConfig,
  ImageModelBinding,
  ImageModelRole,
} from "../../api/client";
import { api, settingsQuery } from "../../api/queries";
import { Icon } from "../../components/Icon";

const workflowStates: Array<{ state: string; label: string; role: ImageModelRole; roleLabel: string }> = [
  { state: "intake_clarify", label: "需求澄清", role: "reasoning_llm", roleLabel: "Reasoning LLM" },
  { state: "confirmation_build", label: "确认稿构建", role: "reasoning_llm", roleLabel: "Reasoning LLM" },
  { state: "initial_candidate_generation", label: "首轮候选生成", role: "text_to_image_model", roleLabel: "文生图" },
  { state: "self_check_inspection", label: "自检审阅", role: "vision_language_model", roleLabel: "VLM" },
  { state: "self_check_rework", label: "自检返工", role: "text_to_image_model", roleLabel: "文生图" },
  { state: "human_prompt_rework", label: "人工反馈返工", role: "text_to_image_model", roleLabel: "文生图" },
];

function operationId(prefix: string): string {
  return `${prefix}_${crypto.randomUUID().replaceAll("-", "")}`;
}

function editableConfig(config: GlobalConfig): Omit<GlobalConfig, "revision"> {
  const { revision: _revision, ...editable } = config;
  return editable;
}

function MutationFeedback({ pending, error, success }: { pending: boolean; error: string | null; success: string | null }): React.JSX.Element | null {
  if (pending) return <p className="settings-feedback" role="status" aria-live="polite">正在校验并安全保存…</p>;
  if (error) return <p className="settings-feedback settings-feedback--error" role="alert">{error}<br />当前已保存修订未被覆盖，请修正后重试。</p>;
  if (success) return <p className="settings-feedback settings-feedback--success" role="status" aria-live="polite">{success}</p>;
  return null;
}

function PaidSmokeDialog({
  credential,
  model,
  pending,
  error,
  onCancel,
  onConfirm,
}: {
  credential: CredentialSummary;
  model: string;
  pending: boolean;
  error: string | null;
  onCancel: () => void;
  onConfirm: () => void;
}): React.JSX.Element {
  const dialogRef = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    dialogRef.current?.showModal();
    return () => dialogRef.current?.close();
  }, []);
  return (
    <dialog ref={dialogRef} className="settings-smoke-dialog" aria-labelledby="paid-smoke-title" onCancel={(event) => { event.preventDefault(); if (!pending) onCancel(); }}>
      <div className="workbench-drawer__header"><div><p className="workbench-eyebrow">真实 Provider 诊断</p><h2 id="paid-smoke-title">确认一次付费生图</h2></div><button type="button" className="workbench-icon-button" aria-label="关闭付费诊断窗口" disabled={pending} onClick={onCancel}><Icon name="close" /></button></div>
      <div className="settings-smoke-dialog__body">
        <p className="settings-cost-warning" role="note"><Icon name="status" /><span><strong>此操作会向 Ark 发起一次真实图片生成并可能产生费用。</strong>普通保存和配置预检永远不会隐式触发此请求。</span></p>
        <dl className="workbench-definition-list"><div><dt>凭据</dt><dd>{credential.credential_pair_id} · {credential.key_id} · 尾号 {credential.key_tail}</dd></div><div><dt>模型</dt><dd>{model}</dd></div><div><dt>数量</dt><dd>1 张最小诊断图片</dd></div></dl>
        {error ? <p className="workbench-inline-error" role="alert">{error}<br />系统不会自动重试。再次运行需要重新勾选费用确认。</p> : null}
      </div>
      <footer className="workbench-dialog-actions settings-smoke-dialog__actions"><button type="button" className="workbench-secondary-button" disabled={pending} onClick={onCancel}>取消</button><button type="button" className="settings-paid-button" disabled={pending} aria-busy={pending} onClick={onConfirm}>{pending ? "正在等待 Ark 返回…" : "确认费用并生成 1 张"}</button></footer>
    </dialog>
  );
}

export function SettingsPage(): React.JSX.Element {
  const queryClient = useQueryClient();
  const settings = useQuery(settingsQuery);
  const config = settings.data?.globalConfig.config;
  const credentials = settings.data?.keyPool.items ?? [];
  const [credentialId, setCredentialId] = useState("ark_primary");
  const [keyId, setKeyId] = useState("ark_key_primary");
  const [baseUrl, setBaseUrl] = useState("https://ark.cn-beijing.volces.com/api/v3");
  const [apiKey, setApiKey] = useState("");
  const [credentialRevision, setCredentialRevision] = useState(1);
  const [showSecret, setShowSecret] = useState(false);
  const [modelConfigId, setModelConfigId] = useState("ark_image_v1");
  const [bindings, setBindings] = useState<ImageModelBinding[]>([]);
  const [runtime, setRuntime] = useState({ questionPreference: "proactive" as "proactive" | "blocking_only", candidateConcurrency: 5, defaultOutputSize: "2560x1440", responseFormat: "url" as "url" | "b64_json", watermark: false, offlineMode: false });
  const [credentialSuccess, setCredentialSuccess] = useState<string | null>(null);
  const [configSuccess, setConfigSuccess] = useState<string | null>(null);
  const [costConfirmed, setCostConfirmed] = useState(false);
  const [smokeCredentialId, setSmokeCredentialId] = useState("");
  const [smokeDialog, setSmokeDialog] = useState(false);
  const smokeTriggerRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!config) return;
    setModelConfigId(config.image_model_config.model_config_id);
    setBindings(workflowStates.map((definition) => config.image_model_config.state_bindings.find((item) => item.state === definition.state) ?? {
      state: definition.state,
      model_role: definition.role,
      provider: "ark",
      model: "",
      parameters: {},
      fallback_model: null,
    }));
    setRuntime({
      questionPreference: config.image_runtime_policy.question_preference,
      candidateConcurrency: config.image_runtime_policy.candidate_concurrency,
      defaultOutputSize: config.image_runtime_policy.default_output_size,
      responseFormat: config.image_runtime_policy.response_format,
      watermark: config.image_runtime_policy.watermark,
      offlineMode: config.image_runtime_policy.offline_mode,
    });
  }, [config]);

  useEffect(() => {
    const firstArk = credentials.find((item) => item.provider === "ark" && item.enabled);
    if (firstArk && !smokeCredentialId) setSmokeCredentialId(firstArk.credential_pair_id);
  }, [credentials, smokeCredentialId]);

  const refreshSettings = async (): Promise<void> => {
    await queryClient.invalidateQueries({ queryKey: settingsQuery.queryKey });
  };
  const saveCredentials = useMutation({
    mutationFn: () => api.updateKeyPool({
      pairs: [{
        credential_pair_id: credentialId,
        provider: "ark",
        key_id: keyId,
        base_url: baseUrl,
        api_key: apiKey,
        api_key_env: "ARK_API_KEY",
        base_url_env: "ARK_BASE_URL",
        revision: credentialRevision,
        enabled: true,
      }],
      envelope: { idempotency_key: operationId("credential"), actor_type: "human", actor_id: "human_operator", expected_revision: 0 },
    }),
    onSuccess: async () => {
      setApiKey("");
      setShowSecret(false);
      setCredentialSuccess("Ark 凭据已保存；明文 Key 已从表单清除，页面只保留脱敏摘要。");
      await refreshSettings();
    },
  });
  const saveConfig = useMutation({
    mutationFn: async (next: Omit<GlobalConfig, "revision">) => {
      if (!config) throw new Error("全局配置尚未加载。");
      const requestId = operationId("global_config");
      return api.updateGlobalConfig({
        config: next,
        operation_id: requestId,
        envelope: { idempotency_key: requestId, actor_type: "human", actor_id: "human_operator", expected_revision: config.revision },
      });
    },
    onSuccess: async (response) => {
      setConfigSuccess(`全局配置已保存为 r${response.config.revision}；诊断结果需重新执行。`);
      preflight.reset();
      await refreshSettings();
    },
  });
  const preflight = useMutation({
    mutationFn: () => {
      if (!config) throw new Error("全局配置尚未加载。");
      return api.preflightSettings(config.revision);
    },
  });
  const paidSmoke = useMutation({
    mutationFn: () => {
      if (!config) throw new Error("全局配置尚未加载。");
      const credential = credentials.find((item) => item.credential_pair_id === smokeCredentialId && item.enabled);
      if (!credential) throw new Error("请选择一个启用的 Ark 凭据对。");
      const requestId = operationId("paid_smoke");
      return api.runPaidSmoke({
        credential_pair_id: credential.credential_pair_id,
        credential_pair_revision: credential.revision,
        cost_confirmation: true,
        operation_id: requestId,
        envelope: { idempotency_key: requestId, actor_type: "human", actor_id: "human_operator", expected_revision: config.revision },
      });
    },
    onSuccess: () => {
      setSmokeDialog(false);
      setCostConfirmed(false);
      window.requestAnimationFrame(() => smokeTriggerRef.current?.focus());
    },
    onError: () => {
      setSmokeDialog(false);
      setCostConfirmed(false);
      window.requestAnimationFrame(() => smokeTriggerRef.current?.focus());
    },
  });

  const updateBinding = (state: string, patch: Partial<ImageModelBinding>): void => {
    setBindings((current) => current.map((item) => item.state === state ? { ...item, ...patch } : item));
  };
  const selectedSmokeCredential = credentials.find((item) => item.credential_pair_id === smokeCredentialId);
  const smokeModel = bindings.find((item) => item.state === "initial_candidate_generation")?.model ?? "未配置";

  return (
    <section className="workbench-page settings-page" aria-labelledby="settings-title">
      <header className="workbench-page__header"><div><p className="workbench-eyebrow">正式运行入口</p><h1 id="settings-title">Ark 与 Image Agent 设置</h1><p>凭据、六状态模型路由、运行策略和诊断均在受控修订中完成；明文 Key 不会再次返回。</p></div>{config ? <span className="settings-revision">全局 r{config.revision}</span> : null}</header>
      {settings.isPending ? <div className="task-projection__loading" role="status">正在读取脱敏配置…</div> : null}
      {settings.isError ? <div className="task-projection__error" role="alert"><strong>无法读取设置</strong><span>{settings.error.message}</span><button type="button" className="workbench-secondary-button" onClick={() => void settings.refetch()}>重新读取</button></div> : null}
      {config ? <div className="settings-sections">
        <section className="settings-panel" aria-labelledby="provider-credentials-title">
          <header><div><p className="workbench-eyebrow">Provider 凭据</p><h2 id="provider-credentials-title">Ark Key Pair</h2><p>Key 和 Base URL 始终作为同一不可拆分凭据对保存。</p></div><span>{credentials.filter((item) => item.enabled).length} 个启用</span></header>
          <div className="settings-credential-list" aria-label="脱敏凭据列表">{credentials.length ? credentials.map((item) => <article key={`${item.credential_pair_id}-${item.revision}`}><div><strong>{item.credential_pair_id}</strong><span className={item.enabled ? "settings-ready" : "settings-muted"}>{item.enabled ? "已启用" : "已停用"}</span></div><dl><div><dt>Provider</dt><dd>{item.provider}</dd></div><div><dt>Key ID</dt><dd>{item.key_id}</dd></div><div><dt>Key 尾号</dt><dd>{item.key_tail}</dd></div><div><dt>服务地址</dt><dd>{item.base_url_hint}</dd></div><div><dt>修订</dt><dd>r{item.revision}</dd></div></dl></article>) : <p>尚未配置凭据对。</p>}</div>
          <form className="settings-form" onSubmit={(event) => { event.preventDefault(); setCredentialSuccess(null); saveCredentials.mutate(); }}>
            <div className="settings-form-grid"><label><span>凭据对 ID</span><input required pattern="[A-Za-z][A-Za-z0-9_-]{0,127}" value={credentialId} onChange={(event) => setCredentialId(event.currentTarget.value)} /></label><label><span>Key ID</span><input required pattern="[A-Za-z][A-Za-z0-9_-]{0,127}" value={keyId} onChange={(event) => setKeyId(event.currentTarget.value)} /></label><label className="settings-form-wide"><span>Ark Base URL</span><input required type="url" value={baseUrl} onChange={(event) => setBaseUrl(event.currentTarget.value)} onBlur={() => setBaseUrl((value) => value.trim().replace(/\/$/, ""))} aria-describedby="ark-url-help" /><small id="ark-url-help">只接受不含用户名、密码、查询串或片段的 HTTP(S) 服务根地址。</small></label><label><span>凭据修订</span><input required type="number" min={1} value={credentialRevision} onChange={(event) => setCredentialRevision(event.currentTarget.valueAsNumber || 1)} /></label><label className="settings-secret-field"><span>Ark API Key</span><span><input required type={showSecret ? "text" : "password"} autoComplete="new-password" value={apiKey} onChange={(event) => setApiKey(event.currentTarget.value)} /><button type="button" className="workbench-secondary-button" aria-pressed={showSecret} onClick={() => setShowSecret((value) => !value)}>{showSecret ? "隐藏" : "显示"}</button></span></label></div>
            <p className="settings-form-note">保存会替换当前启用凭据池；修改既有凭据时必须递增 revision。</p>
            <div className="settings-form-actions"><button type="submit" className="workbench-primary-button" disabled={saveCredentials.isPending || !apiKey.trim()}>{saveCredentials.isPending ? "正在安全保存…" : "安全保存 Ark 凭据"}</button></div>
            <MutationFeedback pending={saveCredentials.isPending} error={saveCredentials.isError ? saveCredentials.error.message : null} success={credentialSuccess} />
          </form>
        </section>

        <section className="settings-panel" aria-labelledby="model-routes-title">
          <header><div><p className="workbench-eyebrow">模型路由</p><h2 id="model-routes-title">六状态完整绑定</h2><p>每个状态的能力类型固定；保存前会校验 Provider 一致性和缺失模型。</p></div><span>6 / 6</span></header>
          <form className="settings-form" onSubmit={(event) => { event.preventDefault(); setConfigSuccess(null); if (bindings.some((item) => !item.model.trim())) return; saveConfig.mutate({ ...editableConfig(config), image_provider: "ark", image_model_config: { model_config_id: modelConfigId, state_bindings: bindings.map((item) => ({ ...item, provider: "ark", model: item.model.trim(), fallback_model: item.fallback_model?.trim() || null })) } }); }}>
            <label className="settings-model-id"><span>模型配置 ID</span><input required pattern="[A-Za-z][A-Za-z0-9_-]{0,127}" value={modelConfigId} onChange={(event) => setModelConfigId(event.currentTarget.value)} /></label>
            <div className="settings-route-table" role="group" aria-label="六状态模型路由">{workflowStates.map((definition) => { const binding = bindings.find((item) => item.state === definition.state); return <fieldset key={definition.state}><legend>{definition.label}</legend><code>{definition.state}</code><label><span>能力</span><input value={definition.roleLabel} readOnly aria-readonly="true" /></label><label><span>Ark 模型 ID</span><input required value={binding?.model ?? ""} onChange={(event) => updateBinding(definition.state, { model: event.currentTarget.value, model_role: definition.role, provider: "ark" })} onBlur={(event) => event.currentTarget.setCustomValidity(event.currentTarget.value.trim() ? "" : "请填写 Ark 模型 ID")} /></label><label><span>回退模型（可选）</span><input value={binding?.fallback_model ?? ""} onChange={(event) => updateBinding(definition.state, { fallback_model: event.currentTarget.value || null })} /></label></fieldset>; })}</div>
            <div className="settings-form-actions"><button type="submit" className="workbench-primary-button" disabled={saveConfig.isPending || bindings.some((item) => !item.model.trim())}>{saveConfig.isPending ? "正在保存路由…" : "保存六状态模型路由"}</button></div>
            <MutationFeedback pending={saveConfig.isPending} error={saveConfig.isError ? saveConfig.error.message : null} success={configSuccess} />
          </form>
        </section>

        <section className="settings-panel" aria-labelledby="runtime-policy-title">
          <header><div><p className="workbench-eyebrow">Image Agent 运行策略</p><h2 id="runtime-policy-title">受控生成参数</h2><p>运行中实例无法热应用的变更会明确标记需重启。</p></div></header>
          <form className="settings-form" onSubmit={(event) => { event.preventDefault(); setConfigSuccess(null); saveConfig.mutate({ ...editableConfig(config), image_runtime_policy: { ...config.image_runtime_policy, question_preference: runtime.questionPreference, candidate_concurrency: runtime.candidateConcurrency, default_output_size: runtime.defaultOutputSize, response_format: runtime.responseFormat, watermark: runtime.watermark, offline_mode: runtime.offlineMode } }); }}>
            <div className="settings-form-grid"><label><span>提问策略</span><select value={runtime.questionPreference} onChange={(event) => setRuntime((value) => ({ ...value, questionPreference: event.currentTarget.value as typeof value.questionPreference }))}><option value="proactive">主动澄清</option><option value="blocking_only">仅阻塞时提问</option></select></label><label><span>候选并发</span><input type="number" min={1} max={5} value={runtime.candidateConcurrency} onChange={(event) => setRuntime((value) => ({ ...value, candidateConcurrency: event.currentTarget.valueAsNumber }))} /></label><label><span>默认输出尺寸</span><input required pattern="(\d{2,5}x\d{2,5}|[124]K)" value={runtime.defaultOutputSize} onChange={(event) => setRuntime((value) => ({ ...value, defaultOutputSize: event.currentTarget.value }))} /></label><label><span>响应格式</span><select value={runtime.responseFormat} onChange={(event) => setRuntime((value) => ({ ...value, responseFormat: event.currentTarget.value as typeof value.responseFormat }))}><option value="url">URL</option><option value="b64_json">Base64 JSON</option></select></label><label className="settings-checkbox"><input type="checkbox" checked={runtime.watermark} onChange={(event) => setRuntime((value) => ({ ...value, watermark: event.currentTarget.checked }))} /><span>添加水印</span></label><label className="settings-checkbox"><input type="checkbox" checked={runtime.offlineMode} onChange={(event) => setRuntime((value) => ({ ...value, offlineMode: event.currentTarget.checked }))} /><span>离线模式（真实 Ark 运行前关闭）</span></label></div>
            <div className="settings-form-actions"><button type="submit" className="workbench-primary-button" disabled={saveConfig.isPending}>{saveConfig.isPending ? "正在保存策略…" : "保存运行策略"}</button></div>
          </form>
        </section>

        <section className="settings-panel" aria-labelledby="diagnostics-title">
          <header><div><p className="workbench-eyebrow">诊断</p><h2 id="diagnostics-title">零费用预检与显式付费 smoke</h2><p>预检只读取已保存的脱敏配置；真实生图必须单独确认。</p></div>{preflight.data ? <span className={preflight.data.status === "READY" ? "settings-ready" : "settings-blocked"}>{preflight.data.status === "READY" ? "预检通过" : "预检阻塞"}</span> : null}</header>
          <div className="settings-diagnostic-actions"><button type="button" className="workbench-primary-button" disabled={preflight.isPending} onClick={() => preflight.mutate()}>{preflight.isPending ? "正在执行零费用预检…" : "运行配置预检（不生图）"}</button>{preflight.isError ? <p className="workbench-inline-error" role="alert">{preflight.error.message}<br />重新读取最新设置后再运行预检。</p> : null}</div>
          {preflight.data ? <ul className="settings-checks" aria-label="配置预检结果">{preflight.data.checks.map((check) => <li className={check.status === "PASS" ? "settings-check--pass" : "settings-check--blocked"} key={check.check_id}><Icon name={check.status === "PASS" ? "file-check" : "status"} /><span><strong>{check.message}</strong>{check.recovery ? <small>{check.recovery}</small> : null}</span></li>)}</ul> : null}
          <div className="settings-paid-smoke"><h3>真实 Ark 付费 smoke</h3><p>仅在零费用预检通过后可用。失败不会自动重试，避免产生不可见的重复费用。</p><label><span>使用凭据</span><select value={smokeCredentialId} onChange={(event) => setSmokeCredentialId(event.currentTarget.value)}><option value="">选择启用的 Ark 凭据</option>{credentials.filter((item) => item.provider === "ark" && item.enabled).map((item) => <option value={item.credential_pair_id} key={item.credential_pair_id}>{item.credential_pair_id} · {item.key_id} · 尾号 {item.key_tail}</option>)}</select></label><label className="settings-checkbox settings-cost-confirm"><input type="checkbox" checked={costConfirmed} onChange={(event) => setCostConfirmed(event.currentTarget.checked)} /><span>我确认下一步会产生一次真实图片生成费用</span></label><button ref={smokeTriggerRef} type="button" className="settings-paid-button" disabled={preflight.data?.status !== "READY" || !costConfirmed || !selectedSmokeCredential} onClick={() => { paidSmoke.reset(); setSmokeDialog(true); }}>打开付费 smoke 确认</button>{paidSmoke.isError ? <p className="workbench-inline-error" role="alert">{paidSmoke.error.message}<br />系统未自动重试；再次运行必须重新勾选费用确认。</p> : null}{paidSmoke.data ? <p className="settings-smoke-success" role="status" aria-live="polite">Ark 真实 smoke 已通过：模型 {paidSmoke.data.model}，返回 {paidSmoke.data.generated_count} 张结果，耗时 {paidSmoke.data.duration_ms} ms。</p> : null}</div>
        </section>
      </div> : null}
      {smokeDialog && selectedSmokeCredential ? <PaidSmokeDialog credential={selectedSmokeCredential} model={smokeModel} pending={paidSmoke.isPending} error={paidSmoke.isError ? paidSmoke.error.message : null} onCancel={() => { if (paidSmoke.isPending) return; setSmokeDialog(false); window.requestAnimationFrame(() => smokeTriggerRef.current?.focus()); }} onConfirm={() => paidSmoke.mutate()} /> : null}
    </section>
  );
}
