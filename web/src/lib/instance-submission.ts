export type InstanceChoice = {
  accountId?: string | null;
  instanceId?: string | null;
};

export type InstanceSnapshot = {
  id?: string;
  instance_id?: string;
  state?: string | null;
  account_id?: string | null;
  current_job_id?: string | null;
  instance_label?: string | null;
  label?: string | null;
  gpu_reported?: string | null;
};

export type SubmissionDecision = {
  ok: boolean;
  allocation: 'auto' | 'existing';
  accountId: string | null;
  instanceId: string | null;
  message: string;
  machineLabel: string;
};

const REUSABLE = new Set(['ready', 'retained_idle']);

export function instanceIdentity(instance: InstanceSnapshot): string {
  return instance.id || instance.instance_id || '';
}

export function instanceReusable(instance: InstanceSnapshot | null | undefined): boolean {
  if (!instance) return false;
  const state = String(instance.state || '').toLowerCase();
  if (!REUSABLE.has(state)) return false;
  if (instance.current_job_id) return false;
  return true;
}

export function instanceMachineLabel(instance: InstanceSnapshot | null | undefined): string {
  if (!instance) return '未选择';
  const name = instance.instance_label || instance.label || instanceIdentity(instance).slice(0, 8) || '机器';
  const gpu = instance.gpu_reported ? ` · ${instance.gpu_reported}` : '';
  const state = instance.state ? ` · ${instance.state}` : '';
  return `${name}${gpu}${state}`;
}

/**
 * A stale or released machine must not be submitted, and must not be silently
 * replaced by another account. The caller keeps the current account and asks
 * the user to confirm automatic allocation on a later click.
 */
export function resolveInstanceSubmission(choice: InstanceChoice, instances: InstanceSnapshot[]): SubmissionDecision {
  const accountId = choice.accountId || null;
  const instanceId = choice.instanceId || null;
  if (!instanceId) {
    return {
      ok: true,
      allocation: 'auto',
      accountId,
      instanceId: null,
      message: '',
      machineLabel: '自动分配',
    };
  }
  const selected = instances.find((instance) => instanceIdentity(instance) === instanceId) || null;
  if (instanceReusable(selected) && selected) {
    const owner = selected.account_id || null;
    if (accountId && owner && accountId !== owner) {
      return {
        ok: false,
        allocation: 'auto',
        accountId,
        instanceId: null,
        machineLabel: instanceMachineLabel(selected),
        message: '所选机器不属于当前账号。这次没有提交任务，也没有改用其他账号。请改选这台机器对应的账号，或清空机器后重新确认。',
      };
    }
    return {
      ok: true,
      allocation: 'existing',
      accountId: accountId || owner,
      instanceId,
      message: '',
      machineLabel: instanceMachineLabel(selected),
    };
  }
  const keptAccount = accountId || selected?.account_id || null;
  return {
    ok: false,
    allocation: 'auto',
    accountId: keptAccount,
    instanceId: null,
    machineLabel: selected ? instanceMachineLabel(selected) : '已失效的机器',
    message: keptAccount
      ? '所选 GPU 机器已释放或不能复用。这次没有提交任务，也没有换账号。请再点一次确认，按当前账号自动分配机器，或另选一台可用机器。'
      : '所选 GPU 机器已释放或不能复用。这次没有提交任务。请先选定账号，或再点一次确认后按已授权账号自动分配。',
  };
}
