import type { Provider } from '../lib/gpu-api';
import { instanceIdentity, instanceMachineLabel, instanceReusable } from '../lib/instance-submission';
import type { GpuAccount, GpuInstance } from './AccountDirectory';
import { formatInstanceTime } from '../lib/instance-time';
import { gpuLabel } from '../lib/gpu-label';
import styles from './v020.module.css';

export type RuntimeLifecycle = 'save' | 'batch' | 'smart' | 'retain';

export interface RuntimeSelectionValue {
  accountId: string | null;
  instanceId: string | null;
  lifecycle: RuntimeLifecycle;
  retainIdleMinutes: number;
}

export interface RuntimeSelectionProps {
  value: RuntimeSelectionValue;
  accounts: GpuAccount[];
  instances: GpuInstance[];
  provider: Provider;
  onChange: (value: RuntimeSelectionValue) => void;
}

const lifecycleLabels: Record<RuntimeLifecycle, string> = {
  save: '节省：任务结束后释放',
  batch: '批次：兼容任务复用',
  smart: '智能：短暂空闲保留',
  retain: '保留：继续使用此机器',
};

function accountStatus(account: GpuAccount): string {
  return account.status || 'unknown';
}

function instanceId(instance: GpuInstance): string {
  return instanceIdentity(instance);
}

function isAccountSelectable(account: GpuAccount): boolean {
  const colabConnected = account.authorization?.colab?.connected === true || account.colab?.status === 'connected' || account.colab_status === 'connected';
  return account.enabled !== false && account.allow_allocation === true && accountStatus(account) === 'connected' && colabConnected;
}

function instanceLabel(instance: GpuInstance): string {
  const cutoff = instance.hard_deadline_at || instance.idle_deadline_at;
  return `${instance.instance_label || instance.label || instanceId(instance)} · ${gpuLabel(instance)} · ${instance.state || '状态未知'}${cutoff ? ` · 截止 ${formatInstanceTime(cutoff)}` : ''}`;
}

export function RuntimeSelection({ value, accounts, instances, provider, onChange }: RuntimeSelectionProps) {
  const selectedAccount = accounts.find((account) => account.account_id === value.accountId);
  const sameAccount = instances.filter((instance) => !value.accountId || instance.account_id === value.accountId);
  const reusableInstances = sameAccount.filter((instance) => instanceReusable(instance));
  const staleInstance = value.instanceId ? sameAccount.find((instance) => instanceId(instance) === value.instanceId && !instanceReusable(instance)) : undefined;
  const candidateInstances = staleInstance ? [...reusableInstances, staleInstance] : reusableInstances;
  const selectedInstance = candidateInstances.find((instance) => instanceId(instance) === value.instanceId);
  const hasAccounts = accounts.some(isAccountSelectable);
  const accountReason = !accounts.length ? '尚未加载账号。' : !hasAccounts ? '没有已连接且允许分配的账号；请先在设置中的账号目录完成授权。' : '';
  const instanceReason = value.instanceId && (!selectedInstance || !instanceReusable(selectedInstance)) ? '所选机器已释放或不能复用。提交前会停下来，不会静默改用其他账号。' : '';

  function update(patch: Partial<RuntimeSelectionValue>) {
    onChange({ ...value, ...patch });
  }

  function changeAccount(accountId: string) {
    update({ accountId: accountId || null, instanceId: null });
  }

  return (
    <section className={styles.selection} aria-label="运行选择">
      <div className={styles.selectionPanel}>
        <div>
          <p className={styles.eyebrow}>运行设置</p>
          <h3>选择账号与机器</h3>
        </div>
        <div className={styles.selectionRow}>
          <label htmlFor="runtime-account">账号
            <select id="runtime-account" value={value.accountId || ''} onChange={(event) => changeAccount(event.target.value)} disabled={!hasAccounts}>
              <option value="">自动选择（当前 owner 允许的账号）</option>
              {accounts.map((account) => <option key={account.account_id} value={account.account_id} disabled={!isAccountSelectable(account)}>{account.alias || account.account_id} · {accountStatus(account) === 'connected' ? '可用' : accountStatus(account)}</option>)}
            </select>
          </label>
          {accountReason && <span className={styles.selectionHint}>{accountReason}</span>}
        </div>
        <div className={styles.selectionRow}>
          <label htmlFor="runtime-instance">机器
            <select id="runtime-instance" value={value.instanceId || ''} onChange={(event) => update({ instanceId: event.target.value || null })} disabled={(!candidateInstances.length && Boolean(value.accountId))}>
              <option value="">自动分配兼容机器</option>
              {candidateInstances.map((instance) => <option key={instanceId(instance)} value={instanceId(instance)} disabled={!instanceReusable(instance)}>{instanceReusable(instance) ? instanceLabel(instance) : `${instanceMachineLabel(instance)} · 不能复用`}</option>)}
            </select>
          </label>
          {selectedAccount && <span className={styles.selectionHint}>当前账号：{selectedAccount.alias}。指定机器时不会静默切换到其他账号。</span>}
          {instanceReason && <span className={styles.selectionHint}>{instanceReason}</span>}
        </div>
        <div className={styles.selectionRow}>
          <label htmlFor="runtime-lifecycle">任务完成后的策略
            <select id="runtime-lifecycle" value={value.lifecycle} onChange={(event) => update({ lifecycle: event.target.value as RuntimeLifecycle })}>
              {(Object.keys(lifecycleLabels) as RuntimeLifecycle[]).map((lifecycle) => <option key={lifecycle} value={lifecycle}>{lifecycleLabels[lifecycle]}</option>)}
            </select>
          </label>
        </div>
        {(value.lifecycle === 'smart' || value.lifecycle === 'retain') && <div className={styles.selectionRow}>
          <label htmlFor="retain-idle-minutes">空闲保留时间（分钟）
            <input id="retain-idle-minutes" type="number" min={1} max={30} step={1} value={value.retainIdleMinutes} onChange={(event) => update({ retainIdleMinutes: Math.max(1, Math.min(30, Number(event.target.value) || 1)) })} />
          </label>
          <p className={styles.cuHint}>保留期间仍会累计站内估算 CU；官方实际扣费来源未知，不显示为 0，也不承诺 Colab 永久在线。硬截止仍由服务端控制。</p>
        </div>}
        {provider === 'mock' && <p className={styles.selectionHint}>Mock 只用于离线验收，不会启动真实 GPU 或消耗 CU。</p>}
      </div>
    </section>
  );
}
