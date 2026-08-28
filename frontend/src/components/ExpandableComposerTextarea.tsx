import { useState, type Ref, type TextareaHTMLAttributes } from "react";
import { Icon } from "./Icon";

interface ExpandableComposerTextareaProps extends Omit<TextareaHTMLAttributes<HTMLTextAreaElement>, "rows"> {
  id: string;
  label: string;
  textareaRef?: Ref<HTMLTextAreaElement>;
}

export function ExpandableComposerTextarea({
  id,
  label,
  textareaRef,
  ...textareaProps
}: ExpandableComposerTextareaProps): React.JSX.Element {
  const [expanded, setExpanded] = useState(false);
  const actionLabel = expanded ? "收起消息输入框" : "展开消息输入框";

  return (
    <div className="master-composer__field">
      <label htmlFor={id}>{label}</label>
      <div className="master-composer__textarea">
        <textarea
          {...textareaProps}
          id={id}
          ref={textareaRef}
          rows={expanded ? 12 : 4}
        />
        <button
          type="button"
          className="workbench-icon-button master-composer__expand"
          aria-controls={id}
          aria-expanded={expanded}
          aria-label={actionLabel}
          title={actionLabel}
          onClick={() => setExpanded((value) => !value)}
        >
          <Icon name={expanded ? "collapse" : "expand"} />
        </button>
      </div>
    </div>
  );
}
