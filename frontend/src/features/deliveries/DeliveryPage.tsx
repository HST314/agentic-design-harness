import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import type { TaskFile } from "../../api/client";
import { api, sharedDeliveryFilesQuery } from "../../api/queries";
import { Icon } from "../../components/Icon";
import { formatBytes } from "../../ui";
import { TaskTabs } from "../master-thread/MasterThreadPage";

const SHARED_PREFIX = "resources/shared/";

function MarkdownPreview({ markdown }: { markdown: string }): React.JSX.Element {
  const blocks = markdown.split(/\n{2,}/).filter((item) => item.trim());
  return (
    <div className="delivery-markdown" aria-label="设计说明预览">
      {blocks.map((block, index) => {
        const value = block.trim();
        const heading = value.match(/^(#{1,3})\s+(.+)$/s);
        if (heading) {
          const content = heading[2];
          if (heading[1]?.length === 1) return <h3 key={index}>{content}</h3>;
          return <h4 key={index}>{content}</h4>;
        }
        const lines = value.split("\n");
        if (lines.every((line) => /^[-*]\s+/.test(line))) {
          return <ul key={index}>{lines.map((line) => <li key={line}>{line.replace(/^[-*]\s+/, "")}</li>)}</ul>;
        }
        if (/^```/.test(value) && /```$/.test(value)) {
          return <pre key={index}><code>{value.replace(/^```[^\n]*\n?/, "").replace(/```$/, "")}</code></pre>;
        }
        return <p key={index}>{lines.map((line, lineIndex) => <span key={`${lineIndex}-${line}`}>{line}{lineIndex < lines.length - 1 ? <br /> : null}</span>)}</p>;
      })}
    </div>
  );
}

export interface DeliveryGalleryItem {
  file: TaskFile;
  notePath: string | null;
}

function designNotePathFor(imagePath: string): string {
  const dot = imagePath.lastIndexOf(".");
  return dot > SHARED_PREFIX.length ? `${imagePath.slice(0, dot)}.md` : `${imagePath}.md`;
}

export function buildGalleryItems(entries: TaskFile[]): DeliveryGalleryItem[] {
  const notePaths = new Set(
    entries
      .filter((entry) => entry.mime_type === "text/markdown")
      .map((entry) => entry.relative_path),
  );
  return entries
    .filter((entry) => (
      entry.relative_path.startsWith(SHARED_PREFIX)
      && entry.mime_type.startsWith("image/")
    ))
    .map((file) => {
      const candidate = designNotePathFor(file.relative_path);
      return { file, notePath: notePaths.has(candidate) ? candidate : null };
    })
    .sort((left, right) => left.file.relative_path.localeCompare(right.file.relative_path));
}

function isUserVisibleSharedFile(entry: TaskFile): boolean {
  if (!entry.relative_path.startsWith(SHARED_PREFIX)) return false;
  return entry.relative_path
    .slice(SHARED_PREFIX.length)
    .split("/")
    .every((segment) => segment.length > 0 && !segment.startsWith("."));
}

export function buildFileListItems(entries: TaskFile[]): TaskFile[] {
  return entries
    .filter(isUserVisibleSharedFile)
    .sort((left, right) => left.relative_path.localeCompare(right.relative_path));
}

export function sharedDisplayName(file: TaskFile): string {
  return file.relative_path.slice(SHARED_PREFIX.length);
}

export function DeliveryFileList({
  taskId,
  items,
}: {
  taskId: string;
  items: TaskFile[];
}): React.JSX.Element {
  return (
    <ul className="delivery-file-list" aria-label="共享文件夹文件列表">
      {items.map((file) => (
        <li className="delivery-file-item" key={file.relative_path}>
          <div className="delivery-file-item__icon" aria-hidden="true"><Icon name="file" /></div>
          <div className="delivery-file-item__body">
            <strong title={sharedDisplayName(file)}>{sharedDisplayName(file)}</strong>
            <small>{file.mime_type} · {formatBytes(file.size_bytes)}</small>
          </div>
          <a
            className="workbench-secondary-button delivery-file-item__download"
            href={api.downloadUrl(taskId, file.relative_path)}
            aria-label={`下载 ${sharedDisplayName(file)}`}
          >
            <Icon name="download" />下载
          </a>
        </li>
      ))}
    </ul>
  );
}

type DeliveryView = "gallery" | "files";

export function DeliveryViewTabs({
  view,
  onChange,
}: {
  view: DeliveryView;
  onChange: (view: DeliveryView) => void;
}): React.JSX.Element {
  const tabs = [
    ["gallery", "图片画廊"],
    ["files", "文件列表"],
  ] as const;
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const onKeyDown = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const index = tabs.findIndex(([key]) => key === view);
    const next = (index + (event.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length;
    const target = tabs[next];
    if (!target) return;
    onChange(target[0]);
    tabRefs.current[next]?.focus();
  };
  return (
    <div className="delivery-view-tabs" role="tablist" aria-label="交付内容视图">
      {tabs.map(([key, label], index) => (
        <button
          key={key}
          ref={(node) => { tabRefs.current[index] = node; }}
          id={`delivery-view-tab-${key}`}
          type="button"
          role="tab"
          aria-selected={view === key}
          aria-controls={`delivery-view-panel-${key}`}
          tabIndex={view === key ? 0 : -1}
          onClick={() => onChange(key)}
          onKeyDown={onKeyDown}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

export function DeliveryGallery({
  taskId,
  items,
  onZoom = () => undefined,
  onShowNote = () => undefined,
}: {
  taskId: string;
  items: DeliveryGalleryItem[];
  onZoom?: (file: TaskFile) => void;
  onShowNote?: (item: DeliveryGalleryItem) => void;
}): React.JSX.Element {
  return (
    <div className="delivery-gallery" aria-label="共享资源图片画廊">
      {items.map((item) => (
        <article className="delivery-tile" key={item.file.relative_path}>
          <button
            type="button"
            className="delivery-tile__thumb"
            aria-label={`放大查看 ${item.file.filename}`}
            onClick={() => onZoom(item.file)}
          >
            <img
              src={api.previewUrl(taskId, item.file.relative_path)}
              alt={item.file.filename}
              loading="lazy"
            />
          </button>
          {item.notePath ? (
            <div className="delivery-tile__actions">
              <button type="button" className="workbench-secondary-button" onClick={() => onShowNote(item)}>
                <Icon name="file" />设计理念
              </button>
            </div>
          ) : null}
        </article>
      ))}
    </div>
  );
}

function DeliveryLightbox({
  taskId,
  file,
  onClose,
}: {
  taskId: string;
  file: TaskFile;
  onClose: () => void;
}): React.JSX.Element {
  const dialogRef = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    dialogRef.current?.showModal();
    return () => dialogRef.current?.close();
  }, []);
  return (
    <dialog
      ref={dialogRef}
      className="delivery-dialog delivery-lightbox"
      aria-labelledby="delivery-lightbox-title"
      onClose={onClose}
    >
      <div className="workbench-drawer__header">
        <div>
          <p className="workbench-eyebrow">交付图片</p>
          <h2 id="delivery-lightbox-title">{file.filename}</h2>
        </div>
        <button type="button" className="workbench-icon-button" aria-label="关闭大图查看" onClick={onClose}>
          <Icon name="close" />
        </button>
      </div>
      <div className="delivery-lightbox__body">
        <img src={api.previewUrl(taskId, file.relative_path)} alt={`${file.filename} 原尺寸大图`} />
      </div>
    </dialog>
  );
}

function DeliveryNoteDialog({
  taskId,
  item,
  onClose,
}: {
  taskId: string;
  item: DeliveryGalleryItem;
  onClose: () => void;
}): React.JSX.Element {
  const dialogRef = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    dialogRef.current?.showModal();
    return () => dialogRef.current?.close();
  }, []);
  const note = useQuery({
    queryKey: ["delivery-shared-note", taskId, item.notePath],
    queryFn: () => api.previewText(taskId, item.notePath as string),
    retry: false,
  });
  return (
    <dialog
      ref={dialogRef}
      className="delivery-dialog delivery-note-dialog"
      aria-label="设计理念"
      onClose={onClose}
    >
      <div className="workbench-drawer__header">
        <div>
          <p className="workbench-eyebrow">设计理念</p>
        </div>
        <button type="button" className="workbench-icon-button" aria-label="关闭设计理念" onClick={onClose}>
          <Icon name="close" />
        </button>
      </div>
      <div className="delivery-dialog__body">
        {note.isPending ? <p className="delivery-note-state" role="status">正在读取设计说明…</p> : null}
        {note.isError ? (
          <p className="delivery-note-state delivery-note-state--error" role="alert">
            <strong>设计说明读取失败</strong>
            <span>{note.error.message}</span>
            <button type="button" className="workbench-secondary-button" onClick={() => void note.refetch()}>重新读取</button>
          </p>
        ) : null}
        {note.data !== undefined ? <MarkdownPreview markdown={note.data} /> : null}
      </div>
    </dialog>
  );
}

export function DeliveryPage(): React.JSX.Element {
  const { taskId = "" } = useParams();
  const files = useQuery(sharedDeliveryFilesQuery(taskId));
  const [view, setView] = useState<DeliveryView>("gallery");
  const [zoomed, setZoomed] = useState<TaskFile | null>(null);
  const [noteTarget, setNoteTarget] = useState<DeliveryGalleryItem | null>(null);
  const items = useMemo(
    () => buildGalleryItems(files.data?.items ?? []),
    [files.data?.items],
  );
  const fileItems = useMemo(
    () => buildFileListItems(files.data?.items ?? []),
    [files.data?.items],
  );

  return (
    <section className="workbench-page deliveries-page" aria-label="任务交付">
      <TaskTabs taskId={taskId} />
      <div className="delivery-view-bar">
        <DeliveryViewTabs view={view} onChange={setView} />
        <div className="delivery-view-bar__actions">
          <button
            type="button"
            className="workbench-secondary-button delivery-refresh"
            disabled={files.isFetching}
            aria-label="刷新共享文件"
            onClick={() => void files.refetch()}
          >
            <Icon name="retry" />{files.isFetching ? "刷新中…" : "刷新"}
          </button>
          <a className="workbench-secondary-button delivery-download" href={api.sharedArchiveUrl(taskId)}>
            <Icon name="download" />下载全部 (zip)
          </a>
        </div>
      </div>
      {files.isPending ? <div className="task-projection__loading" role="status">正在读取共享资源…</div> : null}
      {files.isError ? (
        <div className="task-projection__error" role="alert">
          <strong>无法读取共享资源</strong>
          <span>请检查连接后重试。</span>
          <button type="button" className="workbench-secondary-button" onClick={() => void files.refetch()}>重新读取</button>
        </div>
      ) : null}
      {files.data && view === "gallery" && items.length === 0 ? (
        <div className="task-projection__empty">
          <Icon name="file-check" />
          <h2>暂无已交付的图片</h2>
          <p>图片助手完成交付后，生成的图片会显示在这里；右上角可打包下载共享文件夹的全部内容。</p>
        </div>
      ) : null}
      {files.data && view === "files" && fileItems.length === 0 ? (
        <div className="task-projection__empty">
          <Icon name="file" />
          <h2>共享文件夹暂无文件</h2>
          <p>助手产出的文件会显示在这里；右上角可打包下载共享文件夹的全部内容。</p>
        </div>
      ) : null}
      {view === "gallery" && items.length > 0 ? (
        <div id="delivery-view-panel-gallery" role="tabpanel" aria-labelledby="delivery-view-tab-gallery">
          <DeliveryGallery taskId={taskId} items={items} onZoom={setZoomed} onShowNote={setNoteTarget} />
        </div>
      ) : null}
      {view === "files" && fileItems.length > 0 ? (
        <div id="delivery-view-panel-files" role="tabpanel" aria-labelledby="delivery-view-tab-files">
          <DeliveryFileList taskId={taskId} items={fileItems} />
        </div>
      ) : null}
      {zoomed ? <DeliveryLightbox taskId={taskId} file={zoomed} onClose={() => setZoomed(null)} /> : null}
      {noteTarget?.notePath ? <DeliveryNoteDialog taskId={taskId} item={noteTarget} onClose={() => setNoteTarget(null)} /> : null}
    </section>
  );
}
