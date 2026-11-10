import { defaultParams, parameterKey, parameterList } from './ui-helpers';
import type { GpuManifest, GpuParameter } from './gpu-api';

export type DraftParams = Record<string, unknown>;

export type CarriedDraft = {
  prompt?: string;
  width?: number;
  height?: number;
};

function specOf(manifest: GpuManifest | null | undefined): Map<string, GpuParameter> {
  const specs = new Map<string, GpuParameter>();
  parameterList(manifest || null).forEach((parameter, index) => {
    specs.set(parameterKey(parameter, index), parameter);
  });
  return specs;
}

export function promptKeyOf(keys: string[]): string | null {
  return keys.find((key) => key === 'prompt')
    || keys.find((key) => /prompt|caption/i.test(key) && !/negative/i.test(key))
    || null;
}

export function carriedDraft(params: DraftParams | null | undefined): CarriedDraft {
  const source = params || {};
  const promptKey = promptKeyOf(Object.keys(source));
  const prompt = promptKey && typeof source[promptKey] === 'string' ? source[promptKey].trim() : '';
  const width = typeof source.width === 'number' && Number.isFinite(source.width) ? source.width : undefined;
  const height = typeof source.height === 'number' && Number.isFinite(source.height) ? source.height : undefined;
  return { prompt: prompt || undefined, width, height };
}

function choicesOf(parameter: GpuParameter): string[] {
  const raw = parameter.options || (parameter as { variants?: Record<string, unknown> }).variants;
  if (Array.isArray(raw)) return raw.map((option) => typeof option === 'object' && option && 'value' in option ? String(option.value) : String(option));
  if (raw && typeof raw === 'object') return Object.keys(raw);
  return [];
}

/** Return a value the target workflow can accept, or undefined when it must stay at the default. */
export function coerceDraftValue(parameter: GpuParameter, value: unknown): unknown | undefined {
  const kind = parameter.type;
  if (kind === 'integer' || kind === 'number') {
    const numeric = typeof value === 'number' ? value : typeof value === 'string' && value.trim() ? Number(value) : Number.NaN;
    if (!Number.isFinite(numeric)) return undefined;
    if (kind === 'integer' && !Number.isInteger(numeric)) return undefined;
    if (typeof parameter.min === 'number' && numeric < parameter.min) return undefined;
    if (typeof parameter.max === 'number' && numeric > parameter.max) return undefined;
    if (typeof parameter.step === 'number' && parameter.step > 0 && typeof parameter.min === 'number') {
      const steps = (numeric - parameter.min) / parameter.step;
      if (Math.abs(steps - Math.round(steps)) > 1e-6) return undefined;
    }
    return numeric;
  }
  if (kind === 'boolean') return typeof value === 'boolean' ? value : undefined;
  if (kind === 'image' || kind === 'video' || kind === 'file') return typeof value === 'string' && value ? value : undefined;
  const choices = choicesOf(parameter);
  if (choices.length && !choices.includes(String(value))) return undefined;
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return value;
  return undefined;
}

function applySaved(defaults: DraftParams, specs: Map<string, GpuParameter>, saved: DraftParams): DraftParams {
  const next = { ...defaults };
  for (const [key, parameter] of specs) {
    if (!Object.prototype.hasOwnProperty.call(saved, key)) continue;
    const coerced = coerceDraftValue(parameter, saved[key]);
    if (coerced !== undefined) next[key] = coerced;
  }
  return next;
}

/**
 * Restore a workflow's own draft when one exists.
 * The first visit to a workflow keeps a shared prompt and a compatible size
 * instead of replacing them with that workflow's defaults.
 */
export function mergeWorkflowDraft(
  manifest: GpuManifest | null | undefined,
  saved: DraftParams | null | undefined,
  carried: CarriedDraft | null | undefined,
): DraftParams {
  const defaults = defaultParams(manifest || null);
  const specs = specOf(manifest);
  if (saved && Object.keys(saved).length) return applySaved(defaults, specs, saved);
  const next = { ...defaults };
  const promptKey = promptKeyOf([...specs.keys()]);
  if (promptKey && carried?.prompt) {
    const coerced = coerceDraftValue(specs.get(promptKey)!, carried.prompt);
    if (coerced !== undefined) next[promptKey] = coerced;
  }
  if (specs.has('width') && specs.has('height') && carried?.width !== undefined && carried.height !== undefined) {
    const width = coerceDraftValue(specs.get('width')!, carried.width);
    const height = coerceDraftValue(specs.get('height')!, carried.height);
    if (width !== undefined && height !== undefined) {
      next.width = width;
      next.height = height;
    }
  }
  return next;
}

const memoryDrafts = new Map<string, DraftParams>();

export function draftStorageKey(projectId: string, workflowId: string): string {
  return `studio-draft:${projectId}:${workflowId}`;
}

export function readWorkflowDraft(projectId: string, workflowId: string): DraftParams | null {
  const key = draftStorageKey(projectId, workflowId);
  const cached = memoryDrafts.get(key);
  if (cached) return { ...cached };
  try {
    const raw = sessionStorage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as unknown;
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null;
    return parsed as DraftParams;
  } catch {
    return null;
  }
}

export function writeWorkflowDraft(projectId: string, workflowId: string, params: DraftParams): void {
  if (!projectId || !workflowId) return;
  const key = draftStorageKey(projectId, workflowId);
  const copy = { ...params };
  memoryDrafts.set(key, copy);
  try { sessionStorage.setItem(key, JSON.stringify(copy)); } catch { /* storage can be full or blocked */ }
}
