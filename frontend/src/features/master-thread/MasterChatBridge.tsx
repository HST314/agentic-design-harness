import { Icon } from "../../components/Icon";

export function PendingUserMessage({
  prompt,
  attachmentCount = 0,
}: {
  prompt: string;
  attachmentCount?: number;
}): React.JSX.Element {
  return (
    <article
      className="master-message master-message--user master-message--text master-message--pending"
      aria-label="用户消息，正在发送"
    >
      <header>
        <span>用户消息</span>
        <span>正在发送</span>
      </header>
      <p>{prompt}</p>
      {attachmentCount > 0 ? (
        <ul className="master-message__assets" aria-label="引用资源">
          <li><Icon name="file-check" />{attachmentCount} 个附件随首条消息上传</li>
        </ul>
      ) : null}
    </article>
  );
}

export function PendingThinking({
  title,
  detail,
}: {
  title: string;
  detail?: string;
}): React.JSX.Element {
  return (
    <div className="master-thinking" role="status" aria-live="polite">
      <span className="master-mini-card__spinner" aria-hidden="true" />
      <span><strong>{title}</strong>{detail ? <small>{detail}</small> : null}</span>
    </div>
  );
}

export function DisabledMasterComposer({
  placeholder,
}: {
  placeholder: string;
}): React.JSX.Element {
  return (
    <form className="master-composer" aria-disabled="true" onSubmit={(event) => event.preventDefault()}>
      <div className="master-composer__field">
        <div className="master-composer__field-header">
          <label htmlFor="master-message-pending">发送给 Master</label>
        </div>
        <div className="master-composer__textarea">
          <textarea id="master-message-pending" rows={4} disabled placeholder={placeholder} aria-label="发送给 Master" />
        </div>
      </div>
      <footer>
        <span>{placeholder}</span>
        <button type="submit" className="workbench-primary-button" disabled aria-label="发送 Master 消息">发送消息</button>
      </footer>
    </form>
  );
}

export function MasterChatBridge({
  prompt,
  attachmentCount = 0,
  statusTitle,
  statusDetail,
  error = null,
}: {
  prompt: string;
  attachmentCount?: number;
  statusTitle: string;
  statusDetail?: string;
  error?: string | null;
}): React.JSX.Element {
  return (
    <section className="workbench-page master-workspace" aria-label="Master 对话准备中">
      <div className="master-thread" role="log" aria-live="polite" aria-label="Master 消息记录">
        {prompt ? <PendingUserMessage prompt={prompt} attachmentCount={attachmentCount} /> : null}
        <PendingThinking title={statusTitle} detail={statusDetail} />
        {error ? <p className="workbench-inline-error" role="alert">{error}</p> : null}
      </div>
      <DisabledMasterComposer placeholder="首次材料提交后即可继续与 Master 对话…" />
    </section>
  );
}
