type DeviceRecord = {
  gpu_reported?: string | null;
  runtime_spec_requested?: { accelerator?: string; variant?: string } | null;
  provider?: string | null;
  prep?: boolean | null;
};

/** Measured GPU first; otherwise the requested spec, marked as not measured. */
export function gpuLabel(value: DeviceRecord): string {
  if (value.provider === 'mock') return '模拟 GPU';
  if (value.gpu_reported) return `实测 GPU：${value.gpu_reported}`;
  if (value.prep) return 'CPU 预热';
  const spec = value.runtime_spec_requested;
  if (spec?.accelerator && spec.accelerator !== 'NONE') return `申请 GPU：${spec.accelerator}（未实测）`;
  if (spec?.variant === 'VARIANT_DEFAULT' || spec?.variant === 'VARIANT_CPU') return 'CPU';
  return 'GPU 未记录';
}

export function canRetainJobInstance(job: { instance_id?: string | null; instance_state?: string | null; runtime_deleted?: boolean | null }): boolean {
  return Boolean(job.instance_id) && !job.runtime_deleted && ['ready', 'busy', 'retained_idle'].includes(job.instance_state || '');
}
