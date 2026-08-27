import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import type {
  CommandEnvelope,
  TaskIntakeAsset,
  TaskIntakeMutationResponse,
  TaskIntakeResponse,
} from "../../api/client";
import { ApiError } from "../../api/client";
import { api, taskHistoryQuery, taskIntakeQuery } from "../../api/queries";
import { Icon } from "../../components/Icon";
import { FoundationPage } from "../workbench/FoundationPage";

const ACTOR_ID = "human_operator";
const MAX_FILES = 20;
const MAX_TOTAL_BYTES = 200 * 1024 * 1024;
const MIME_BY_EXTENSION: Record<string, string> = {
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  png: "image/png",
  webp: "image/webp",
  pdf: "application/pdf",
  txt: "text/plain",
  md: "text/markdown",
  markdown: "text/markdown",
};
const LIMIT_BY_MIME: Record<string, number> = {
  "image/jpeg": 20 * 1024 * 1024,
  "image/png": 20 * 1024 * 1024,
  "image/webp": 20 * 1024 * 1024,
  "application/pdf": 50 * 1024 * 1024,
  "text/plain": 5 * 1024 * 1024,
  "text/markdown": 5 * 1024 * 1024,
};

type UploadStatus = "queued" | "uploading" | "failed";

interface LocalUpload {
  id: string;
  file: File;
  declaredMimeType: string;
  description: string;
  progress: number;
  status: UploadStatus;
  error: string | null;
}

interface IntakeLocationState {
  pendingUploads?: LocalUpload[];
  autoSubmit?: boolean;
}

function operationId(prefix: string): string {
  const suffix = typeof crypto.randomUUID === "function"
    ? crypto.randomUUID().replaceAll("-", "")
    : `${Date.now()}${Math.random().toString(16).slice(2)}`;
  return `${prefix}_${suffix}`.slice(0, 128);
}

function envelope(key: string, expectedRevision: number): CommandEnvelope {
  return {
    idempotency_key: key,
    actor_type: "human",
    actor_id: ACTOR_ID,
    expected_revision: expectedRevision,
  };
}

function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
}

function validateFiles(
  files: File[],
  currentCount: number,
  currentBytes: number,
): { uploads: LocalUpload[]; error: string | null } {
  if (currentCount + files.length > MAX_FILES) {
    return { uploads: [], error: "每个任务最多上传 20 个文件。" };
  }
  let total = currentBytes;
  const uploads: LocalUpload[] = [];
  for (const file of files) {
    const extension = file.name.split(".").at(-1)?.toLowerCase() ?? "";
    const mime = MIME_BY_EXTENSION[extension];
    if (!mime) {
      return { uploads: [], error: `${file.name}：仅支持图片、PDF、TXT 和 MD。` };
    }
    if (file.type && file.type !== mime && !(mime === "text/markdown" && file.type === "text/plain")) {
      return { uploads: [], error: `${file.name}：文件声明类型与扩展名不一致。` };
    }
    const limit = LIMIT_BY_MIME[mime];
    if (limit === undefined) {
      return { uploads: [], error: `${file.name}：无法确认文件大小限制。` };
    }
    if (file.size > limit) {
      return {
        uploads: [],
        error: `${file.name}：超过该类型 ${formatBytes(limit)} 的单文件限制。`,
      };
    }
    total += file.size;
    if (total > MAX_TOTAL_BYTES) {
      return { uploads: [], error: "本任务附件总量不能超过 200 MiB。" };
    }
    uploads.push({
      id: operationId("upload"),
      file,
      declaredMimeType: mime,
      description: "",
      progress: 0,
      status: "queued",
      error: null,
    });
  }
  return { uploads, error: null };
}

function FilePicker({
  disabled,
  onFiles,
}: {
  disabled?: boolean;
  onFiles: (files: File[]) => void;
}): React.JSX.Element {
  return (
    <label className={`workbench-file-picker${disabled ? " workbench-file-picker--disabled" : ""}`}>
      <Icon name="upload" />
      <span>添加图片 / PDF / TXT / MD</span>
      <input
        type="file"
        multiple
        disabled={disabled}
        accept=".jpg,.jpeg,.png,.webp,.pdf,.txt,.md,.markdown,image/jpeg,image/png,image/webp,application/pdf,text/plain,text/markdown"
        onChange={(event) => {
          onFiles(Array.from(event.currentTarget.files ?? []));
          event.currentTarget.value = "";
        }}
      />
    </label>
  );
}

function LocalUploadList({
  items,
  onDescription,
  onCancel,
  onRetry,
}: {
  items: LocalUpload[];
  onDescription: (id: string, value: string) => void;
  onCancel: (id: string) => void;
  onRetry: (id: string) => void;
}): React.JSX.Element | null {
  if (items.length === 0) return null;
  return (
    <div className="workbench-upload-list" aria-label="待上传文件">
      {items.map((item) => (
        <article className="workbench-upload-item" key={item.id}>
          <div className="workbench-upload-item__icon"><Icon name="file" /></div>
          <div className="workbench-upload-item__body">
            <div className="workbench-upload-item__heading">
              <strong>{item.file.name}</strong>
              <span>{formatBytes(item.file.size)}</span>
            </div>
            <label>
              <span>文件说明（可选）</span>
              <input
                value={item.description}
                maxLength={4_000}
                disabled={item.status === "uploading"}
                placeholder="说明这份材料的用途"
                onChange={(event) => onDescription(item.id, event.currentTarget.value)}
              />
            </label>
            {item.status === "uploading" ? (
              <div className="workbench-upload-progress">
                <progress value={item.progress} max={100} aria-label={`${item.file.name} 上传进度`} />
                <span>{item.progress}%</span>
              </div>
            ) : null}
            {item.status === "queued" ? <p className="workbench-file-status">等待上传</p> : null}
            {item.error ? <p className="workbench-inline-error" role="alert">{item.error}</p> : null}
          </div>
          <div className="workbench-upload-item__actions">
            {item.status === "failed" ? (
              <button type="button" className="workbench-icon-button" aria-label={`重试 ${item.file.name}`} onClick={() => onRetry(item.id)}><Icon name="retry" /></button>
            ) : null}
            <button type="button" className="workbench-icon-button" aria-label={`${item.status === "uploading" ? "取消上传" : "移除"} ${item.file.name}`} onClick={() => onCancel(item.id)}><Icon name="trash" /></button>
          </div>
        </article>
      ))}
    </div>
  );
}

function ServerAssetList({
  assets,
  locked,
  removingId,
  onRemove,
}: {
  assets: TaskIntakeAsset[];
  locked: boolean;
  removingId: string | null;
  onRemove: (asset: TaskIntakeAsset) => void;
}): React.JSX.Element | null {
  if (assets.length === 0) return null;
  return (
    <div className="workbench-upload-list" aria-label="已上传文件">
      {assets.map((asset) => (
        <article className="workbench-upload-item workbench-upload-item--complete" key={asset.asset_id}>
          <div className="workbench-upload-item__icon"><Icon name="file-check" /></div>
          <div className="workbench-upload-item__body">
            <div className="workbench-upload-item__heading">
              <strong>{asset.filename}</strong>
              <span>{formatBytes(asset.size_bytes)}</span>
            </div>
            <p>{asset.description || "未填写说明"}</p>
            <p className="workbench-file-status workbench-file-status--success">已安全上传 · {asset.mime_type}</p>
          </div>
          {!locked ? (
            <button
              type="button"
              className="workbench-icon-button"
              disabled={removingId === asset.asset_id}
              aria-label={`移除已上传文件 ${asset.filename}`}
              onClick={() => onRemove(asset)}
            >
              <Icon name="trash" />
            </button>
          ) : null}
        </article>
      ))}
    </div>
  );
}

function NewTaskIntakePage(): React.JSX.Element {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [prompt, setPrompt] = useState("");
  const [uploads, setUploads] = useState<LocalUpload[]>([]);
  const [fileError, setFileError] = useState<string | null>(null);
  const create = useMutation({
    mutationFn: () => api.createTaskIntake({
      prompt: prompt.trim(),
      envelope: envelope(operationId("create_intake"), 0),
    }),
    onSuccess: (response) => {
      queryClient.setQueryData(taskIntakeQuery(response.task.task_id).queryKey, response);
      void queryClient.invalidateQueries({ queryKey: taskHistoryQuery.queryKey });
      navigate(`/tasks/${encodeURIComponent(response.task.task_id)}/master`, {
        replace: true,
        state: { pendingUploads: uploads, autoSubmit: true } satisfies IntakeLocationState,
      });
    },
  });
  const sent = create.isPending || create.isSuccess;
  const localBytes = uploads.reduce((sum, item) => sum + item.file.size, 0);

  const addFiles = (files: File[]): void => {
    const result = validateFiles(files, uploads.length, localBytes);
    setFileError(result.error);
    if (result.uploads.length) setUploads((current) => [...current, ...result.uploads]);
  };

  const send = (): void => {
    if (!prompt.trim() || sent) return;
    create.mutate();
  };

  return (
    <section className="workbench-page intake-chat" aria-labelledby="task-intake-title">
      <h1 id="task-intake-title" className="sr-only">创建新的设计任务</h1>
      <div className="master-thread intake-chat__thread" role="log" aria-label="新任务对话">
        <p className="master-thread__empty">描述你的设计目标与交付要求，Master 会在对话中为你生成执行计划。</p>
      </div>
      <form
        className="master-composer intake-chat__composer"
        onSubmit={(event) => {
          event.preventDefault();
          send();
        }}
      >
        {uploads.length ? (
          <ul className="intake-chat__files" aria-label="待随首条消息上传的附件">
            {uploads.map((item) => (
              <li key={item.id}>
                <Icon name="file" />
                <span>{item.file.name}</span>
                <small>{formatBytes(item.file.size)}</small>
                <button
                  type="button"
                  aria-label={`移除 ${item.file.name}`}
                  disabled={sent}
                  onClick={() => setUploads((current) => current.filter((entry) => entry.id !== item.id))}
                >
                  <Icon name="close" />
                </button>
              </li>
            ))}
          </ul>
        ) : null}
        <div className="intake-chat__row">
          {!sent ? (
            <label className="workbench-icon-button intake-chat__attach" title="图片 20 MiB、PDF 50 MiB、文本 5 MiB；仅创建时可添加">
              <Icon name="upload" />
              <input
                type="file"
                multiple
                className="sr-only"
                aria-label="添加附件（图片 / PDF / TXT / MD）"
                accept=".jpg,.jpeg,.png,.webp,.pdf,.txt,.md,.markdown,image/jpeg,image/png,image/webp,application/pdf,text/plain,text/markdown"
                onChange={(event) => {
                  addFiles(Array.from(event.currentTarget.files ?? []));
                  event.currentTarget.value = "";
                }}
              />
            </label>
          ) : null}
          <textarea
            id="task-prompt"
            aria-label="发送给 Master 的首条消息"
            rows={3}
            value={prompt}
            maxLength={20_000}
            placeholder="描述你的设计任务，例如：为秋季发布会生成三套主视觉方向…"
            disabled={sent}
            onChange={(event) => setPrompt(event.currentTarget.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault();
                send();
              }
            }}
          />
          <button type="submit" className="workbench-primary-button" aria-label="发送并创建任务" disabled={!prompt.trim() || sent}>
            {create.isPending ? "正在创建…" : "发送"}
          </button>
        </div>
        <footer className="intake-chat__footer">
          <span>{prompt.length.toLocaleString("zh-CN")} / 20,000{uploads.length ? ` · ${uploads.length} 个附件将随首条消息上传` : " · 附件仅可在创建时添加"}</span>
        </footer>
        {fileError ? <p className="workbench-inline-error" role="alert">{fileError}</p> : null}
        {create.isError ? <p className="workbench-inline-error" role="alert">{create.error.message}</p> : null}
      </form>
    </section>
  );
}

function ExistingTaskIntakePage({ taskId }: { taskId: string }): React.JSX.Element {
  const location = useLocation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const initialState = location.state as IntakeLocationState | null;
  const [uploads, setUploads] = useState<LocalUpload[]>(() => initialState?.pendingUploads ?? []);
  const [autoSubmit, setAutoSubmit] = useState<boolean>(() => Boolean(initialState?.autoSubmit));
  const [fileError, setFileError] = useState<string | null>(null);
  const [removingId, setRemovingId] = useState<string | null>(null);
  const controllers = useRef(new Map<string, AbortController>());
  const autoSubmitFired = useRef(false);
  const intake = useQuery(taskIntakeQuery(taskId));
  const revisionRef = useRef(0);

  useEffect(() => {
    if (initialState?.pendingUploads) {
      navigate(location.pathname, { replace: true, state: null });
    }
  }, [initialState?.pendingUploads, location.pathname, navigate]);

  useEffect(() => {
    if (intake.data) revisionRef.current = Math.max(revisionRef.current, intake.data.intake_revision);
  }, [intake.data]);

  useEffect(() => () => {
    controllers.current.forEach((controller) => controller.abort());
  }, []);

  const mergeUploadResult = useCallback((response: TaskIntakeMutationResponse): void => {
    revisionRef.current = Math.max(revisionRef.current, response.intake_revision);
    queryClient.setQueryData<TaskIntakeResponse>(taskIntakeQuery(taskId).queryKey, (current) => {
      if (!current) return current;
      const assets = response.asset && !current.assets.some((item) => item.asset_id === response.asset?.asset_id)
        ? [...current.assets, response.asset]
        : current.assets;
      if (response.intake_revision < current.intake_revision) return { ...current, assets };
      return { ...current, intake: response.intake, intake_revision: response.intake_revision, assets };
    });
    void queryClient.invalidateQueries({ queryKey: taskHistoryQuery.queryKey });
  }, [queryClient, taskId]);

  const uploadOne = useCallback((item: LocalUpload): void => {
    const controller = new AbortController();
    controllers.current.set(item.id, controller);
    void api.uploadTaskIntakeAsset(
      taskId,
      {
        file: item.file,
        declaredMimeType: item.declaredMimeType,
        description: item.description,
        envelope: envelope(item.id, revisionRef.current),
      },
      (progress) => setUploads((current) => current.map((entry) => entry.id === item.id ? { ...entry, progress } : entry)),
      controller.signal,
    ).then((response) => {
      controllers.current.delete(item.id);
      mergeUploadResult(response);
      setUploads((current) => current.filter((entry) => entry.id !== item.id));
    }).catch((error: unknown) => {
      controllers.current.delete(item.id);
      if (error instanceof DOMException && error.name === "AbortError") {
        setUploads((current) => current.filter((entry) => entry.id !== item.id));
        return;
      }
      const message = error instanceof Error ? error.message : "上传失败，请重试。";
      setUploads((current) => current.map((entry) => entry.id === item.id ? { ...entry, status: "failed", error: message, progress: 0 } : entry));
      void queryClient.invalidateQueries({ queryKey: taskIntakeQuery(taskId).queryKey });
    });
  }, [mergeUploadResult, queryClient, taskId]);

  useEffect(() => {
    if (!intake.data || intake.data.intake.status !== "DRAFT") return;
    const uploading = uploads.filter((item) => item.status === "uploading").length;
    const next = uploads.filter((item) => item.status === "queued").slice(0, Math.max(0, 3 - uploading));
    if (!next.length) return;
    const ids = new Set(next.map((item) => item.id));
    setUploads((current) => current.map((item) => ids.has(item.id) ? { ...item, status: "uploading", error: null } : item));
    next.forEach(uploadOne);
  }, [intake.data, uploadOne, uploads]);

  const remove = useMutation({
    mutationFn: (asset: TaskIntakeAsset) => {
      setRemovingId(asset.asset_id);
      return api.removeTaskIntakeAsset(taskId, asset.asset_id, envelope(operationId("remove_asset"), revisionRef.current));
    },
    onSuccess: (response) => {
      revisionRef.current = response.intake_revision;
      queryClient.setQueryData<TaskIntakeResponse>(taskIntakeQuery(taskId).queryKey, (current) => current ? {
        ...current,
        intake: response.intake,
        intake_revision: response.intake_revision,
        assets: current.assets.filter((asset) => asset.asset_id !== response.removed_asset_id),
      } : current);
      setRemovingId(null);
      void queryClient.invalidateQueries({ queryKey: taskHistoryQuery.queryKey });
    },
    onError: () => setRemovingId(null),
  });
  const submit = useMutation({
    mutationFn: () => api.submitTaskIntake(
      taskId,
      intake.data?.task_revision ?? 0,
      envelope(operationId("submit_intake"), revisionRef.current),
    ),
    onSuccess: (response) => {
      revisionRef.current = response.intake_revision;
      queryClient.setQueryData<TaskIntakeResponse>(taskIntakeQuery(taskId).queryKey, (current) => current ? {
        ...current,
        intake: response.intake,
        intake_revision: response.intake_revision,
        task: response.task ?? current.task,
        task_revision: response.task_revision ?? current.task_revision,
        assets: response.assets ?? current.assets,
      } : current);
      void queryClient.invalidateQueries({ queryKey: taskHistoryQuery.queryKey });
    },
    onError: () => setAutoSubmit(false),
  });

  useEffect(() => {
    if (!autoSubmit || autoSubmitFired.current) return;
    if (!intake.data || intake.data.intake.status !== "DRAFT") return;
    const active = uploads.some((item) => item.status === "queued" || item.status === "uploading");
    if (active) return;
    if (uploads.some((item) => item.status === "failed")) {
      setAutoSubmit(false);
      return;
    }
    autoSubmitFired.current = true;
    submit.mutate();
  }, [autoSubmit, intake.data, uploads, submit]);

  if (intake.isPending) {
    return <TaskIntakeFrame badge="正在恢复"><div className="workbench-intake-card" role="status">正在从服务端恢复草稿与已上传材料…</div></TaskIntakeFrame>;
  }
  if (intake.error instanceof ApiError && intake.error.status === 404) {
    return <FoundationPage view="master" />;
  }
  if (intake.isError || !intake.data) {
    return <TaskIntakeFrame badge="恢复失败"><div className="workbench-intake-card"><p className="workbench-inline-error" role="alert">{intake.error?.message ?? "无法读取任务草稿。"}</p><button type="button" className="workbench-secondary-button" onClick={() => void intake.refetch()}>重新读取</button></div></TaskIntakeFrame>;
  }

  const data = intake.data;
  const locked = data.intake.status !== "DRAFT";
  if (autoSubmit && !locked) {
    const total = data.assets.length + uploads.length;
    return (
      <TaskIntakeFrame badge="正在创建">
        <div className="workbench-intake-card intake-chat__progress" role="status">
          <strong>正在上传并提交首次材料…</strong>
          <p>{total ? `已完成 ${data.assets.length} / ${total} 个文件；` : ""}提交后自动进入与 Master 的对话。</p>
          {submit.isError ? <p className="workbench-inline-error" role="alert">{submit.error.message}</p> : null}
        </div>
      </TaskIntakeFrame>
    );
  }
  const activeUploads = uploads.some((item) => item.status === "queued" || item.status === "uploading");
  const failedUploads = uploads.some((item) => item.status === "failed");
  const serverBytes = data.assets.reduce((sum, asset) => sum + asset.size_bytes, 0);
  const localBytes = uploads.reduce((sum, item) => sum + item.file.size, 0);
  const addFiles = (files: File[]): void => {
    const result = validateFiles(files, data.assets.length + uploads.length, serverBytes + localBytes);
    setFileError(result.error);
    if (result.uploads.length) setUploads((current) => [...current, ...result.uploads]);
  };

  return (
    <TaskIntakeFrame badge={locked ? "F1 已提交" : "F1 可恢复草稿"}>
      <div className="workbench-intake-card">
        <div className="workbench-recovery-note" role="status"><Icon name="status" /><div><strong>{locked ? "首次材料已锁定" : "服务端草稿已恢复"}</strong><p>{locked ? "提交后的 Master 消息只能引用这些已有资源，不能追加上传。" : "已上传文件不会因刷新丢失；未完成的浏览器本地文件需重新选择。"}</p></div></div>
        <label className="workbench-field" htmlFor="task-prompt-recovered">
          <span>Prompt</span>
          <textarea id="task-prompt-recovered" rows={6} value={data.intake.prompt} readOnly />
          <small>Prompt 已作为本任务的服务端事实保存。</small>
        </label>
        <section className="workbench-upload-section" aria-labelledby="draft-upload-title">
          <div className="workbench-upload-section__heading">
            <div><p className="workbench-eyebrow">首次材料</p><h2 id="draft-upload-title">{locked ? "已提交附件" : "上传队列"}</h2><p>{data.assets.length} / 20 个 · {formatBytes(serverBytes)} / 200 MiB · 最多 3 个并发</p></div>
            {!locked ? <FilePicker onFiles={addFiles} disabled={data.assets.length + uploads.length >= MAX_FILES} /> : null}
          </div>
          {fileError ? <p className="workbench-inline-error" role="alert">{fileError}</p> : null}
          <ServerAssetList assets={data.assets} locked={locked} removingId={removingId} onRemove={(asset) => remove.mutate(asset)} />
          <LocalUploadList
            items={uploads}
            onDescription={(id, value) => setUploads((current) => current.map((item) => item.id === id ? { ...item, description: value } : item))}
            onCancel={(id) => {
              controllers.current.get(id)?.abort();
              if (!controllers.current.has(id)) setUploads((current) => current.filter((item) => item.id !== id));
            }}
            onRetry={(id) => setUploads((current) => current.map((item) => item.id === id ? { ...item, status: "queued", error: null } : item))}
          />
          {!data.assets.length && !uploads.length ? <p className="workbench-empty-upload">尚未添加附件；Prompt 可单独提交。</p> : null}
          {remove.isError ? <p className="workbench-inline-error" role="alert">{remove.error.message}</p> : null}
        </section>
        <div className="workbench-intake-footer">
          <div><span className="workbench-footer-label">启动方式</span><strong>{data.intake.start_policy === "manual" ? "人工确认计划后运行" : "自动生成计划，人工确认后运行"}</strong></div>
          {!locked ? <button type="button" className="workbench-primary-button" disabled={activeUploads || failedUploads || submit.isPending || remove.isPending} onClick={() => submit.mutate()}>{submit.isPending ? "正在锁定并提交…" : "提交任务材料"}</button> : null}
        </div>
        {failedUploads ? <p className="workbench-inline-error" role="alert">请重试或移除失败文件后再提交。</p> : null}
        {submit.isError ? <p className="workbench-inline-error" role="alert">{submit.error.message}</p> : null}
        {locked ? <div className="workbench-next-phase"><Icon name="message" /><div><strong>已进入 Master 分析阶段</strong><p>F1 已完成创建与首次上传闭环；永久线程和计划确认将在 F2 接入。</p></div></div> : null}
      </div>
    </TaskIntakeFrame>
  );
}

function TaskIntakeFrame({ badge, children }: { badge: string; children: React.ReactNode }): React.JSX.Element {
  return (
    <section className="workbench-page" aria-labelledby="task-intake-title">
      <header className="workbench-page__header">
        <div><p className="workbench-eyebrow">新任务</p><h1 id="task-intake-title">创建新的设计任务</h1><p>Master 将先分析目标与材料，再生成计划供确认。</p></div>
        <span className="workbench-phase-badge"><span aria-hidden="true" />{badge}</span>
      </header>
      {children}
    </section>
  );
}

export function TaskIntakePage(): React.JSX.Element {
  const { taskId } = useParams();
  return taskId ? <ExistingTaskIntakePage taskId={taskId} /> : <NewTaskIntakePage />;
}
