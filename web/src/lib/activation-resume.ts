export type ActivationResumeStep = {
  key: string;
  done?: boolean;
  running?: boolean;
  failed?: boolean;
};

export type ActivationResumeSnapshot = {
  steps?: ActivationResumeStep[];
  smoke?: null | {
    job_id?: string | null;
    state?: string | null;
    error_code?: string;
    error_message?: string;
  };
  smoke_job_completed?: boolean;
  max_runtime_minutes?: number;
  runtime_limit_minutes?: number;
  runtime_limit_seconds?: number;
  max_runtime_seconds?: number;
  runtime_seconds?: number;
  [key: string]: unknown;
};

const ACTIVE_SMOKE_STATES = new Set(['pending', 'queued', 'running']);
const COMPLETED_SMOKE_STATES = new Set(['completed', 'succeeded', 'success']);
const FAILED_SMOKE_STATES = new Set(['failed', 'cancelled', 'canceled', 'error']);

export function getActivationStep(
  snapshot: ActivationResumeSnapshot | null | undefined,
  key: string,
): ActivationResumeStep | undefined {
  return snapshot?.steps?.find((step) => step.key === key);
}

function smokeState(snapshot: ActivationResumeSnapshot | null | undefined): string {
  return String(snapshot?.smoke?.state || '').toLowerCase();
}

function smokeJobId(snapshot: ActivationResumeSnapshot | null | undefined): string {
  return String(snapshot?.smoke?.job_id || '');
}

export function isSmokePolling(snapshot: ActivationResumeSnapshot | null | undefined): boolean {
  return Boolean(smokeJobId(snapshot)) && ACTIVE_SMOKE_STATES.has(smokeState(snapshot));
}

export function isSmokeCompleted(snapshot: ActivationResumeSnapshot | null | undefined): boolean {
  return Boolean(snapshot?.smoke_job_completed) || COMPLETED_SMOKE_STATES.has(smokeState(snapshot));
}

/** A job that can be resumed or accepted; failed jobs may be retried as a new smoke. */
export function hasExistingSmoke(snapshot: ActivationResumeSnapshot | null | undefined): boolean {
  const jobId = smokeJobId(snapshot);
  if (!jobId || FAILED_SMOKE_STATES.has(smokeState(snapshot))) return false;
  return true;
}

export function shouldCreateSmoke(snapshot: ActivationResumeSnapshot | null | undefined): boolean {
  return !hasExistingSmoke(snapshot);
}

export function shouldAcceptSmoke(snapshot: ActivationResumeSnapshot | null | undefined): boolean {
  return hasExistingSmoke(snapshot)
    && isSmokeCompleted(snapshot)
    && getActivationStep(snapshot, 'accept_smoke')?.done !== true;
}

export function terminalSmokeJobId(snapshot: ActivationResumeSnapshot | null | undefined): string | null {
  const jobId = smokeJobId(snapshot);
  const state = smokeState(snapshot);
  return jobId && (COMPLETED_SMOKE_STATES.has(state) || FAILED_SMOKE_STATES.has(state)) ? jobId : null;
}

/** Prefer the server's limit; keep this tolerant while the API field is coordinated. */
export function getSmokeRuntimeMinutes(snapshot: ActivationResumeSnapshot | null | undefined): number | null {
  const source = snapshot || {};
  const minutes = [source.max_runtime_minutes, source.runtime_limit_minutes]
    .find((value) => typeof value === 'number' && Number.isFinite(value) && value > 0);
  if (typeof minutes === 'number') return minutes;

  const seconds = [source.runtime_limit_seconds, source.max_runtime_seconds, source.runtime_seconds]
    .find((value) => typeof value === 'number' && Number.isFinite(value) && value > 0);
  return typeof seconds === 'number' ? seconds / 60 : null;
}

export function appendActivationLog(log: string[], line: string, maxEntries = 40): string[] {
  if (log[log.length - 1] === line) return log;
  const next = [...log, line];
  return next.length > maxEntries ? next.slice(next.length - maxEntries) : next;
}

export type ActivationInputSpec = {
  key: string;
  label?: string;
  description?: string;
  type?: string;
  required?: boolean;
};

export function missingActivationInputs(inputs: ActivationInputSpec[], values: Record<string, string>): string[] {
  return inputs
    .filter((input) => input.required && !values[input.key])
    .map((input) => input.label || input.key);
}

export function activationSmokeParams(inputs: ActivationInputSpec[], values: Record<string, string>): { params: Record<string, string>; assetIds: string[] } {
  const params: Record<string, string> = {};
  const assetIds: string[] = [];
  for (const input of inputs) {
    const value = values[input.key];
    if (!value) continue;
    params[input.key] = value;
    if (!assetIds.includes(value)) assetIds.push(value);
  }
  return { params, assetIds };
}

export type ActivationWatch = {
  workflowId: string;
  projectId: string;
  jobId: string;
  state: string;
  maxRuntimeMinutes: number | null;
};

const WATCH_KEY = 'studio-activation-watch';

export function readActivationWatch(): Record<string, ActivationWatch> {
  try {
    const raw = sessionStorage.getItem(WATCH_KEY);
    const parsed = raw ? JSON.parse(raw) as unknown : {};
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
    return parsed as Record<string, ActivationWatch>;
  } catch {
    return {};
  }
}

export function writeActivationWatch(value: Record<string, ActivationWatch>): void {
  try { sessionStorage.setItem(WATCH_KEY, JSON.stringify(value)); } catch { /* storage can be unavailable */ }
}

export function smokeIdempotencyKey(workflowId: string, projectId: string, assetIds: string[]): string {
  const signature = `${workflowId}:${projectId}:${[...assetIds].sort().join(',')}`;
  const storageKey = `studio-smoke-idem:${signature}`;
  try {
    const existing = sessionStorage.getItem(storageKey);
    if (existing) return existing;
    const created = `smoke-${workflowId}-${typeof crypto !== 'undefined' && crypto.randomUUID ? crypto.randomUUID() : Date.now()}`;
    sessionStorage.setItem(storageKey, created);
    return created;
  } catch {
    return `smoke-${workflowId}-${Date.now()}`;
  }
}
