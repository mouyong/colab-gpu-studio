import { jobStageLabel, jobStageRows } from './job-stage-rows';

export interface TimingDetail { seconds?: number | null; source?: 'events' | 'worker' | string; status?: 'completed' | 'in_progress' | string; }

export interface TimingRow {
  key: string;
  label: string;
  value: string;
  source: string;
  status: string;
}

export function formatDuration(seconds?: number | null): string {
  if (seconds == null || !Number.isFinite(seconds)) return '—';
  const tenths = Math.round(Math.max(0, seconds) * 10);
  const whole = Math.floor(tenths / 10);
  const fraction = tenths % 10;
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const rest = whole % 60;
  const restText = fraction ? `${rest}.${fraction}` : String(rest);
  const parts: string[] = [];
  if (hours) parts.push(`${hours}小时`);
  if (minutes || hours) parts.push(`${minutes}分`);
  parts.push(`${restText}秒`);
  return parts.join('');
}

export function formatTiming(detail?: TimingDetail | null): string {
  if (!detail || detail.status === 'in_progress') return '进行中';
  return formatDuration(detail.seconds);
}

function timingSource(source?: string): string {
  if (source === 'events') return '持久事件';
  if (source === 'worker') return '运行时记录';
  return '历史数值';
}

export function timingRows(job: {
  timings?: Record<string, number>;
  timing_details?: Record<string, TimingDetail>;
} | null | undefined): TimingRow[] {
  const rows = new Map<string, TimingRow>();
  for (const [key, seconds] of Object.entries(job?.timings || {})) {
    if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) continue;
    rows.set(key, {
      key,
      label: jobStageLabel(key),
      value: formatDuration(seconds),
      source: timingSource(),
      status: 'completed',
    });
  }
  for (const [key, detail] of Object.entries(job?.timing_details || {})) {
    if (!detail || typeof detail !== 'object') continue;
    const status = detail.status === 'in_progress' ? 'in_progress' : 'completed';
    const validSeconds = typeof detail.seconds === 'number' && Number.isFinite(detail.seconds) && detail.seconds >= 0;
    if (status !== 'in_progress' && !validSeconds) continue;
    rows.set(key, {
      key,
      label: jobStageLabel(key),
      value: status === 'in_progress' && validSeconds ? `进行中 · 已用${formatDuration(detail.seconds)}` : formatTiming(detail),
      source: timingSource(detail.source),
      status,
    });
  }
  const preferred = jobStageRows(Object.fromEntries([...rows.keys()].map((key) => [key, 0]))).map((row) => row.key);
  const seen = new Set(preferred);
  return [...preferred, ...[...rows.keys()].filter((key) => !seen.has(key))].flatMap((key) => {
    const row = rows.get(key);
    return row ? [row] : [];
  });
}
