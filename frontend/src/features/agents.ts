import type { TaskSummary } from "../api/client";

export interface AgentUiDescriptor {
  agentType: string;
  label: string;
  instanceTitle: string;
  icon: "image" | "layers";
  available: boolean;
  unavailableTitle?: string;
  unavailableDetail?: string;
}
const descriptors: Record<string, AgentUiDescriptor> = {
  general: {
    agentType: "general",
    label: "通用",
    instanceTitle: "通用 Agent 实例",
    icon: "layers",
    available: true,
  },
  image: {
    agentType: "image",
    label: "Image Agent",
    instanceTitle: "Image Agent 实例",
    icon: "image",
    available: true,
  },
  ppt: {
    agentType: "ppt",
    label: "PPT Agent",
    instanceTitle: "PPT Agent 实例",
    icon: "layers",
    available: false,
    unavailableTitle: "PPT Agent 尚未接入",
    unavailableDetail: "必需节点在激活后会保持能力不可用，不会伪装成成功。",
  },
};

export function agentDescriptor(agentType: string): AgentUiDescriptor {
  return descriptors[agentType] ?? {
    agentType,
    label: agentType,
    instanceTitle: "专业 Agent 实例",
    icon: "layers",
    available: true,
  };
}

export function sidebarCapabilityNote(): string {
  const unavailable = Object.values(descriptors).find((item) => !item.available);
  if (!unavailable) return "";
  return '<div class="capability-note"><span class="status-dot status-dot--muted" aria-hidden="true"></span><span><strong>' + unavailable.label + ' 尚未接入</strong><small>计划节点与状态可用</small></span></div>';
}

export function taskCapabilityNotice(task: TaskSummary): string {
  return task.has_unavailable_ppt
    ? '<div class="capability-inline"><span class="status-dot status-dot--muted" aria-hidden="true"></span>PPT 未接入</div>'
    : "";
}

export function stageCapabilityNotice(agentType: string): string {
  const descriptor = agentDescriptor(agentType);
  if (descriptor.available || !descriptor.unavailableTitle) return "";
  return '<div class="alert alert--warning"><strong>' + descriptor.unavailableTitle + '</strong><span>' + (descriptor.unavailableDetail ?? "") + '</span></div>';
}
