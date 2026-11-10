import type { GpuJob } from './gpu-api';
import { friendlyLabel } from './labels';
import { formatDuration } from './job-history';

export function taskStatusNote(job: { state?: string; workflow_smoke?: boolean }): string {
  const done = ['completed', 'succeeded', 'success'].includes(String(job.state || '').toLowerCase());
  if (job.workflow_smoke) {
    return done
      ? '这是工作流启用前的冒烟任务，这次生成已经结束。任务完成不等于工作流已经发布。'
      : '这是工作流启用前的冒烟任务。下面的进度是这次冒烟，不是发布是否完成。';
  }
  return done
    ? '这是一次生成任务。状态「已完成」只表示这次生成结束，不表示工作流已经发布。'
    : '这是一次生成任务的进度。它不表示工作流是否已经发布。';
}

export function paramRows(params: Record<string, unknown> | null | undefined): Array<{ key: string; label: string; value: string }> {
  if (!params) return [];
  return Object.entries(params).flatMap(([key, value]) => {
    if (value === null || value === undefined || value === '') return [];
    const text = typeof value === 'object' ? JSON.stringify(value) : String(value);
    return [{ key, label: friendlyLabel(key, key), value: text }];
  });
}

export function stageSecondsTotal(timings: Record<string, number> | null | undefined): string {
  if (!timings) return '';
  const values = Object.values(timings).filter((value) => typeof value === 'number' && Number.isFinite(value) && value >= 0);
  if (!values.length) return '';
  return formatDuration(values.reduce((sum, value) => sum + value, 0));
}

export function jobDetailTimings(job: GpuJob): Record<string, number> | undefined {
  const value = job.timings;
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined;
  return value as Record<string, number>;
}
