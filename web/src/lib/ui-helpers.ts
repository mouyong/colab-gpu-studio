import type { GpuManifest, GpuParameter } from './gpu-api';

export const terminalStates = new Set(['completed', 'succeeded', 'failed', 'failed_final', 'cancelled', 'cancelled_local', 'blocked']);

export function record(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value));
}

export function parameterList(manifest: GpuManifest | null): GpuParameter[] {
  if (!manifest?.parameters) return [];
  if (Array.isArray(manifest.parameters)) return manifest.parameters;
  return Object.entries(manifest.parameters).map(([key, item]) => {
    const normalized = item as GpuParameter;
    return { ...normalized, key, id: normalized.id || key };
  });
}

export function parameterKey(item: GpuParameter, index: number) {
  return item.key || item.id || item.name || `parameter_${index + 1}`;
}

export function defaultParams(manifest: GpuManifest | null) {
  return Object.fromEntries(
    parameterList(manifest).map((item, index) => [
      parameterKey(item, index),
      item.default !== undefined
        ? item.default
        : item.type === 'boolean'
        ? false
        : item.type === 'integer' || item.type === 'number'
        ? item.min ?? 0
        : '',
    ])
  );
}

export function keyId() {
  return typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `gpu-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export function shown(value: unknown) {
  return value === null || value === undefined || value === '' ? '未知' : typeof value === 'object' ? JSON.stringify(value) : String(value);
}

export function formatCu(value: unknown, isMock: boolean = false): string {
  if (value === null || value === undefined || value === '') return '0.00 CU';
  const num = Number(value);
  if (Number.isNaN(num)) return String(value);
  const formatted = num.toFixed(2);
  return isMock ? `≈ ${formatted} CU（模拟估算）` : `${formatted} CU`;
}

export function formatDateTime(value?: string | number): string {
  if (!value) return '未知时间';
  try {
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return String(value);
    const now = new Date();
    const isToday = d.toDateString() === now.toDateString();
    const pad = (n: number) => String(n).padStart(2, '0');
    const timeStr = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    if (isToday) return `今天 ${timeStr}`;
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${timeStr}`;
  } catch {
    return String(value);
  }
}

export function readableState(value?: string) {
  return value
    ? ({
        queued: '排队中', pending: '等待中', preparing: '准备中', validating: '校验中',
        creating: '创建运行时', reserved: '已预留', active: '运行时已启动', started: '已启动',
        uploading: '上传中', running: '运行中', saving: '保存中', cleanup_pending: '清理中',
        deleted: '已释放', quarantined: '已隔离', completed: '已完成', succeeded: '已完成',
        requested: '申请中', allocating: '分配中', ready: '空闲', idle: '空闲', busy: '运行任务中',
        retained_idle: '保留中', releasing: '释放中', deleting: '释放中', unknown: '状态未知',
        failed: '失败', failed_final: '最终失败', cancelled: '已取消', cancelled_local: '已取消', blocked: '已阻止',
      }[value] || value)
    : '未知';
}

export function workflowState(value?: string, active?: boolean) {
  return active ? '当前使用' : ({ draft: '草稿', validated: '已校验，待发布', published: '已发布' }[value || ''] || value || '未知');
}

export function progressPercent(value?: number) {
  return typeof value === 'number' ? Math.round(Math.max(0, Math.min(1, value)) * 100) : null;
}

export function assetIdsFromParams(params: Record<string, unknown>, validIds: Set<string>) {
  return Object.values(params).filter((value): value is string => typeof value === 'string' && validIds.has(value));
}

/** 数字转 “0.00”，不是数字时给 “—”。 */
export function cuNumber(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—';
  const num = Number(value);
  return Number.isFinite(num) ? num.toFixed(2) : '—';
}

export function jobSize(job: { params?: Record<string, unknown>; resolved_params?: Record<string, unknown> }): string {
  const source = job.resolved_params || job.params || {};
  const w = Number(source.width);
  const h = Number(source.height);
  return w > 0 && h > 0 ? `${w} × ${h}` : '';
}
