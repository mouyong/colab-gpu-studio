import { useState } from 'react';
import { Settings, ChevronLeft, ChevronRight, Monitor, Gauge, Plug, Cpu, Layers, LogOut, Search, Square } from 'lucide-react';
import { WorkflowsPanel } from './WorkflowManager';
import { ThemePicker } from './ComputePage';
import type { ComputeSpec, GpuSettings, GpuUsage, GpuWorkflow } from '../lib/gpu-api';
import { GoogleOAuthSetup } from '../components/GoogleOAuthSetup';
import { AccountDirectory, type GpuAccount } from '../components/AccountDirectory';
import { InstanceCenter } from '../components/InstanceCenter';
import type { GpuInstance } from '../components/AccountDirectory';
import { ComputeSpecs } from '../components/ComputeSpecs';
import { Card } from '../components/ui';
import styles from '../gpu.module.css';
import { cuNumber } from '../lib/ui-helpers';

export function SettingsPanel({
  settings, usage, specs, specsNote = '', workflows, loading, error, accounts = [], onAccountsChange, instances = [], onRefreshInstances,
  projectId = '', projectTitle = '',
  onConnect, onDisconnect, onCheck, onStop, onImport, onValidate, onPublish, onRollback, onDeactivate, onActivated, onOpenJob, onUseProject, onLogout, onBack,
}: {
  settings: GpuSettings | null;
  usage?: GpuUsage | null;
  specs: ComputeSpec[];
  specsNote?: string;
  workflows: GpuWorkflow[];
  loading: string;
  error: string;
  onConnect: (purpose: 'drive' | 'colab') => void;
  onDisconnect: (purpose: 'drive' | 'colab') => void;
  onCheck: () => void;
  onStop: () => void;
  onImport: (bundle: string) => void;
  onValidate: (workflow: GpuWorkflow) => void;
  onPublish: (workflow: GpuWorkflow) => void;
  onRollback: (workflow: GpuWorkflow) => void;
  onDeactivate?: (workflow: GpuWorkflow) => void;
  onActivated?: () => void;
  projectId?: string;
  projectTitle?: string;
  onOpenJob?: (jobId: string, projectId?: string) => void;
  onUseProject?: (projectId: string) => void;
  onLogout: () => void;
  onBack?: () => void;
  instances?: GpuInstance[];
  onRefreshInstances?: () => void;
  accounts?: GpuAccount[];
  onAccountsChange?: (accounts: GpuAccount[]) => void;
}) {
  const [view, setView] = useState<'main' | 'workflows'>('main');
  const [oauthConfigured, setOauthConfigured] = useState(false);

  const activeCount = Number(usage?.active_runtimes ?? 0);
  const dailyCap = Number(usage?.daily_limit || settings?.budget?.daily_units || 0);
  const used = Number(usage?.actual_units || usage?.estimated_units || 0);
  const pct = dailyCap > 0 ? Math.min(100, Math.round((used / dailyCap) * 100)) : 0;
  const familyCount = new Set(workflows.map((w) => w.workflow_key)).size;

  if (view === 'workflows') {
    return (
      <div className={styles.stack}>
        <div className={styles.subHead}>
          <button type="button" className={styles.backBtn} onClick={() => setView('main')}><ChevronLeft size={18} strokeWidth={1.6} aria-hidden />设置</button>
          <h2>工作流与模型</h2>
        </div>
        <WorkflowsPanel workflows={workflows} projectId={projectId} projectTitle={projectTitle} onImport={onImport} onValidate={onValidate} onPublish={onPublish} onRollback={onRollback} onDeactivate={onDeactivate} onActivated={onActivated || (() => undefined)} onOpenJob={onOpenJob} onUseProject={onUseProject} />
        {error && <p className="status error" role="alert">{error}</p>}
      </div>
    );
  }

  return (
    <div className={styles.stack}>
      <div className={styles.subHead}>
        <button type="button" className={styles.backBtn} onClick={onBack}><ChevronLeft size={18} strokeWidth={1.6} aria-hidden />返回</button>
        <h2><Settings size={20} strokeWidth={1.6} aria-hidden />设置</h2>
      </div>

      <Card icon={<Monitor size={17} strokeWidth={1.6} aria-hidden />} title="外观">
        <ThemePicker />
      </Card>

      <Card icon={<Gauge size={17} strokeWidth={1.6} aria-hidden />} title="用量与额度" aside={<span className="pill">已用 {pct}%</span>}>
        <div className={styles.grid2}>
          <div className={styles.statBox}><small className="muted">运行中任务</small><strong>{activeCount}</strong></div>
          <div className={styles.statBox}><small className="muted">每日上限</small><strong>{dailyCap || '—'}<em> CU</em></strong></div>
          <div className={styles.statBox}><small className="muted">已用</small><strong>{cuNumber(used)}<em> CU</em></strong></div>
          <div className={styles.statBox}><small className="muted">使用率</small><strong>{pct}%</strong></div>
        </div>
      </Card>

      <InstanceCenter instances={instances} onRefresh={onRefreshInstances} />

      <Card icon={<Plug size={17} strokeWidth={1.6} aria-hidden />} title="Google 账号" aside={settings?.offline_mode ? <span className="pill">离线模式</span> : undefined}>
        <AccountDirectory accounts={accounts} onAccountsChange={onAccountsChange} />
        {!settings?.offline_mode && (
          <details className="foldable">
            <summary>Google 应用配置（首次使用）</summary>
            <div className="foldContent"><GoogleOAuthSetup onConfigured={() => setOauthConfigured(true)} /></div>
          </details>
        )}
      </Card>

      <Card icon={<Cpu size={17} strokeWidth={1.6} aria-hidden />} title="GPU">
        <div className={styles.twoButtons}>
          <button className="button" style={{ flex: 1 }} disabled={loading === 'check'} type="button" onClick={onCheck}>
            <Search size={15} strokeWidth={1.8} aria-hidden />{loading === 'check' ? '检查中…' : '检查可用 GPU'}
          </button>
          <button className="button danger" style={{ flex: 1 }} disabled={loading === 'stop'} type="button" onClick={onStop}>
            <Square size={13} strokeWidth={1.8} aria-hidden />{loading === 'stop' ? '停止中…' : '停止本地任务'}
          </button>
        </div>
        {specsNote && <p className="muted small" role="status">{specsNote}</p>}
        {specs.length ? <ComputeSpecs specs={specs} /> : null}
      </Card>

      <button type="button" className={styles.navRow} onClick={() => setView('workflows')}>
        <Layers size={18} strokeWidth={1.5} aria-hidden />
        <span><strong>工作流与模型</strong><small className="muted">{familyCount} 个工作流 · {workflows.length} 个版本</small></span>
        <ChevronRight size={18} strokeWidth={1.5} aria-hidden />
      </button>

      <button className="button secondary big" type="button" onClick={onLogout}><LogOut size={17} strokeWidth={1.6} aria-hidden />退出登录</button>
      {error && <p className="status error" role="alert">{error}</p>}
    </div>
  );
}
