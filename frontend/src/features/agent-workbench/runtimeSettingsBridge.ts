export const RUNTIME_SETTINGS_BRIDGE_PROTOCOL = "image-agent-runtime-settings";
export const RUNTIME_SETTINGS_BRIDGE_VERSION = "1.0";

export type RuntimeSettingsBridgeAction =
  | "runtime_settings.get"
  | "runtime_settings.propose"
  | "runtime_settings.confirm"
  | "runtime_settings.sync_toggle";

export interface RuntimeSettingsBridgeRequest {
  protocol: typeof RUNTIME_SETTINGS_BRIDGE_PROTOCOL;
  version: typeof RUNTIME_SETTINGS_BRIDGE_VERSION;
  type: "bridge.request";
  instance_id: string;
  request_id: string;
  nonce: string;
  action: RuntimeSettingsBridgeAction;
  payload: Record<string, unknown>;
}

const IDENTIFIER = /^[A-Za-z][A-Za-z0-9_-]{0,127}$/;
const REQUEST_ID = /^[A-Za-z][A-Za-z0-9_-]{7,127}$/;
const OUTPUT_SIZE = /^(?:[1-9][0-9]{1,4}x[1-9][0-9]{1,4}|[124]K)$/;
const SETTING_FIELDS = new Set([
  "question_preference",
  "max_auto_questions",
  "clarification_total_budget",
  "category_constraint",
  "style_direction",
  "candidate_concurrency",
  "default_output_size",
  "response_format",
  "watermark",
  "self_check",
  "advanced_model_overrides",
]);
const SELF_CHECK_FIELDS = new Set([
  "termination",
  "fixed_rounds",
  "max_rounds",
  "stop_early_on_pass",
]);
const LIBRARY_RELEASE_FIELDS = new Set(["release"]);
const MODEL_FIELDS = new Set([
  "intake_clarify",
  "confirmation_build",
  "initial_candidate_generation",
  "self_check_inspection",
  "self_check_rework",
  "human_prompt_rework",
]);

function record(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function identifiers(value: unknown): value is string[] {
  return Array.isArray(value)
    && value.length <= 100
    && value.every((item) => typeof item === "string" && IDENTIFIER.test(item));
}

function integerOrNull(value: unknown, minimum: number, maximum: number): boolean {
  return value === null || (Number.isInteger(value) && Number(value) >= minimum && Number(value) <= maximum);
}

function enumOrNull(value: unknown, allowed: string[]): boolean {
  return value === null || (typeof value === "string" && allowed.includes(value));
}

function strictRecord(
  value: unknown,
  fields: Set<string>,
  validator: (key: string, item: unknown) => boolean,
): boolean {
  return record(value)
    && Object.keys(value).every((key) => fields.has(key) && validator(key, value[key]));
}

function validSelfCheck(value: unknown): boolean {
  if (value === null) return true;
  return strictRecord(value, SELF_CHECK_FIELDS, (key, item) => {
    if (key === "termination") return enumOrNull(item, ["fix", "solo"]);
    if (key === "fixed_rounds") return integerOrNull(item, 1, 20);
    if (key === "max_rounds") return integerOrNull(item, 1, 50);
    return item === null || typeof item === "boolean";
  });
}

function validLibraryRelease(value: unknown): boolean {
  if (value === null) return true;
  return strictRecord(value, LIBRARY_RELEASE_FIELDS, (_key, item) => (
    enumOrNull(item, ["auto", "manual", "off"])
  ));
}

function validModelOverrides(value: unknown): boolean {
  if (value === null) return true;
  return strictRecord(value, MODEL_FIELDS, (_key, item) => (
    item === null || (typeof item === "string" && item.length >= 1 && item.length <= 128)
  ));
}

function validOverrides(value: unknown): boolean {
  return strictRecord(value, SETTING_FIELDS, (key, item) => {
    if (key === "question_preference") return enumOrNull(item, ["proactive", "blocking_only"]);
    if (key === "max_auto_questions") return integerOrNull(item, 0, 10);
    if (key === "clarification_total_budget") return integerOrNull(item, 0, 100);
    if (key === "category_constraint" || key === "style_direction") {
      return validLibraryRelease(item);
    }
    if (key === "candidate_concurrency") return integerOrNull(item, 1, 5);
    if (key === "default_output_size") {
      return item === null || (typeof item === "string" && item.length <= 64 && OUTPUT_SIZE.test(item));
    }
    if (key === "response_format") return enumOrNull(item, ["url", "b64_json"]);
    if (key === "watermark") return item === null || typeof item === "boolean";
    if (key === "self_check") return validSelfCheck(item);
    return validModelOverrides(item);
  });
}

function validPayload(action: RuntimeSettingsBridgeAction, payload: Record<string, unknown>): boolean {
  if (action === "runtime_settings.get") return Object.keys(payload).length === 0;
  if (action === "runtime_settings.confirm") {
    return Object.keys(payload).length === 1
      && typeof payload.proposal_id === "string"
      && IDENTIFIER.test(payload.proposal_id);
  }
  if (action === "runtime_settings.sync_toggle") {
    return Object.keys(payload).length === 1
      && typeof payload.sync_to_peers === "boolean";
  }
  const allowed = new Set([
    "base_revision",
    "overrides",
    "sync_unstarted_image_work_items",
    "expected_sync_instance_ids",
  ]);
  return Object.keys(payload).length === allowed.size
    && Object.keys(payload).every((key) => allowed.has(key))
    && Number.isInteger(payload.base_revision)
    && Number(payload.base_revision) >= 1
    && validOverrides(payload.overrides)
    && typeof payload.sync_unstarted_image_work_items === "boolean"
    && identifiers(payload.expected_sync_instance_ids);
}

export function isBridgeHello(value: unknown, instanceId: string): boolean {
  return record(value)
    && value.protocol === RUNTIME_SETTINGS_BRIDGE_PROTOCOL
    && value.version === RUNTIME_SETTINGS_BRIDGE_VERSION
    && value.type === "bridge.hello"
    && value.instance_id === instanceId;
}

export function parseBridgeRequest(
  value: unknown,
  instanceId: string,
  nonce: string,
): RuntimeSettingsBridgeRequest | null {
  if (!record(value)
    || value.protocol !== RUNTIME_SETTINGS_BRIDGE_PROTOCOL
    || value.version !== RUNTIME_SETTINGS_BRIDGE_VERSION
    || value.type !== "bridge.request"
    || value.instance_id !== instanceId
    || value.nonce !== nonce
    || typeof value.request_id !== "string"
    || !REQUEST_ID.test(value.request_id)
    || !["runtime_settings.get", "runtime_settings.propose", "runtime_settings.confirm", "runtime_settings.sync_toggle"].includes(String(value.action))
    || !record(value.payload)) return null;
  const action = value.action as RuntimeSettingsBridgeAction;
  if (!validPayload(action, value.payload)) return null;
  return value as unknown as RuntimeSettingsBridgeRequest;
}

export function newBridgeNonce(): string {
  const first = crypto.randomUUID().replaceAll("-", "");
  const second = crypto.randomUUID().replaceAll("-", "");
  return `${first}${second}`;
}

export function bridgeIdempotencyKey(action: RuntimeSettingsBridgeAction, requestId: string): string {
  const intent = action.split(".").at(-1) ?? "request";
  return `workbench_${intent}_${requestId}`.slice(0, 128);
}
