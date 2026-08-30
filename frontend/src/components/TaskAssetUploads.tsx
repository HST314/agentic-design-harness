import { Icon } from "./Icon";

export const MAX_TASK_FILES = 20;
export const MAX_TASK_ASSET_BYTES = 200 * 1024 * 1024;

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

export interface LocalUpload {
  id: string;
  file: File;
  declaredMimeType: string;
  description: string;
  progress: number;
  status: "queued" | "uploading" | "failed";
  error: string | null;
}

function uploadId(): string {
  const suffix = typeof crypto.randomUUID === "function"
    ? crypto.randomUUID().replaceAll("-", "")
    : `${Date.now()}${Math.random().toString(16).slice(2)}`;
  return `upload_${suffix}`.slice(0, 128);
}

export function formatBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KiB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
}

export function validateTaskAssetFiles(
  files: File[],
  currentCount: number,
  currentBytes: number,
): { uploads: LocalUpload[]; error: string | null } {
  if (currentCount + files.length > MAX_TASK_FILES) {
    return { uploads: [], error: "每个任务最多上传 20 个文件。" };
  }
  let total = currentBytes;
  const uploads: LocalUpload[] = [];
  for (const file of files) {
    const extension = file.name.split(".").at(-1)?.toLowerCase() ?? "";
    const mime = MIME_BY_EXTENSION[extension];
    if (!mime) return { uploads: [], error: `${file.name}：仅支持图片、PDF、TXT 和 MD。` };
    if (file.type && file.type !== mime && !(mime === "text/markdown" && file.type === "text/plain")) {
      return { uploads: [], error: `${file.name}：文件声明类型与扩展名不一致。` };
    }
    const limit = LIMIT_BY_MIME[mime];
    if (limit === undefined) {
      return { uploads: [], error: `${file.name}：无法确认文件大小限制。` };
    }
    if (file.size > limit) {
      return { uploads: [], error: `${file.name}：超过该类型 ${formatBytes(limit)} 的单文件限制。` };
    }
    total += file.size;
    if (total > MAX_TASK_ASSET_BYTES) {
      return { uploads: [], error: "本任务附件总量不能超过 200 MiB。" };
    }
    uploads.push({
      id: uploadId(),
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

export function TaskAssetFilePicker({
  disabled,
  onFiles,
}: {
  disabled?: boolean;
  onFiles: (files: File[]) => void;
}): React.JSX.Element {
  return (
    <label
      className={`workbench-icon-button task-asset-file-picker${disabled ? " task-asset-file-picker--disabled" : ""}`}
      title="图片 20 MiB、PDF 50 MiB、文本 5 MiB"
      aria-disabled={disabled || undefined}
    >
      <Icon name="upload" />
      <input
        type="file"
        multiple
        disabled={disabled}
        aria-label="添加附件（图片 / PDF / TXT / MD）"
        accept=".jpg,.jpeg,.png,.webp,.pdf,.txt,.md,.markdown,image/jpeg,image/png,image/webp,application/pdf,text/plain,text/markdown"
        onChange={(event) => {
          onFiles(Array.from(event.currentTarget.files ?? []));
          event.currentTarget.value = "";
        }}
      />
    </label>
  );
}

export function LocalUploadList({
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
            <div className="workbench-upload-item__heading"><strong>{item.file.name}</strong><span>{formatBytes(item.file.size)}</span></div>
            <label>
              <span>文件说明（可选）</span>
              <input value={item.description} maxLength={4_000} disabled={item.status === "uploading"} placeholder="说明这份材料的用途" onChange={(event) => onDescription(item.id, event.currentTarget.value)} />
            </label>
            {item.status === "uploading" ? <div className="workbench-upload-progress"><progress value={item.progress} max={100} aria-label={`${item.file.name} 上传进度`} /><span>{item.progress}%</span></div> : null}
            {item.status === "queued" ? <p className="workbench-file-status">等待上传</p> : null}
            {item.error ? <p className="workbench-inline-error" role="alert">{item.error}</p> : null}
          </div>
          <div className="workbench-upload-item__actions">
            {item.status === "failed" ? <button type="button" className="workbench-icon-button" aria-label={`重试 ${item.file.name}`} onClick={() => onRetry(item.id)}><Icon name="retry" /></button> : null}
            <button type="button" className="workbench-icon-button" aria-label={`${item.status === "uploading" ? "取消上传" : "移除"} ${item.file.name}`} onClick={() => onCancel(item.id)}><Icon name="trash" /></button>
          </div>
        </article>
      ))}
    </div>
  );
}
