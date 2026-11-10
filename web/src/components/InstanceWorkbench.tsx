import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { apiMessage, getWorkbench, workbenchAction, type GpuParameter, type Workbench } from '../lib/gpu-api';
import { getInstanceHistory, type InstanceHistory, type InstanceHistoryJob } from '../lib/instance-history';
import { formatDuration, formatTiming, timingRows, type TimingDetail } from '../lib/job-history';
import { jobStageLabel, jobStateLabel } from '../lib/job-stage-rows';
import { gpuLabel } from '../lib/gpu-label';
import type { GpuInstance } from './AccountDirectory';
import { WorkflowGraphPreview, type WorkflowGraphPreviewData } from './WorkflowGraphPreview';
import styles from './v020.module.css';

export interface ReviewedWorkflow {
  workflow_key: string;
  workflow_version: string | number;
  model_id?: string;
  description?: string;
  mock?: boolean;
  parameters?: GpuParameter[];
  draft?: { values?: Record<string, unknown> };
  [key: string]: unknown;
}
export interface WorkbenchArtifact { id?: string; name?: string; filename?: string; mime?: string; thumbnail_url?: string; status?: string; [key: string]: unknown; }
export interface WorkbenchLog { id?: string; message?: string; created_at?: string; [key: string]: unknown; }
export interface InstanceWorkbenchState extends Workbench {
  allowed_workflows?: ReviewedWorkflow[];
  read_only?: boolean;
  comfy?: { available?: boolean; verified?: boolean };
  artifacts?: WorkbenchArtifact[];
  logs?: Array<WorkbenchLog | string>;
  parameters?: GpuParameter[];
  values?: Record<string, unknown>;
  graph?: unknown;
  [key: string]: unknown;
}
export interface WorkbenchApi {
  getInstanceWorkbench: (instanceId: string) => Promise<InstanceWorkbenchState>;
  getInstanceHistory: (instanceId: string) => Promise<InstanceHistory>;
  workbenchAction: (instanceId: string, action: string, params: Record<string, unknown>, idempotencyKey: string) => Promise<unknown>;
}
export interface InstanceWorkbenchProps { instance: GpuInstance; api?: Partial<WorkbenchApi>; onBack?: () => void; onQueued?: (result: unknown) => void; }

type WorkbenchTab = 'parameters' | 'artifacts' | 'logs' | 'history';

let keySequence = 0;
function newKey(prefix: string) { keySequence += 1; return `${prefix}-${Date.now().toString(36)}-${keySequence.toString(36)}`; }
function instanceId(instance: GpuInstance) { return instance.id || instance.instance_id || ''; }
function record(value: unknown): Record<string, unknown> { return value && typeof value === 'object' ? value as Record<string, unknown> : {}; }
function recordPayload(value: unknown): InstanceWorkbenchState {
  const body = record(value);
  return (body.data && typeof body.data === 'object' ? body.data : value || {}) as InstanceWorkbenchState;
}
function historyPayload(value: unknown): InstanceHistory {
  const body = record(value);
  const payload = body.data && typeof body.data === 'object' ? body.data : value;
  const result = record(payload);
  return { instance_id: String(result.instance_id || ''), offline: result.offline === true, source: String(result.source || 'persisted_job_history'), jobs: Array.isArray(result.jobs) ? result.jobs as InstanceHistoryJob[] : [] };
}
function workflowIdentity(workflow: ReviewedWorkflow) { return `${workflow.workflow_key}@${workflow.workflow_version}`; }
function historyOnly(instance: GpuInstance, state?: InstanceWorkbenchState) {
  return state?.read_only === true || ['unknown', 'deleted', 'releasing', 'cleanup_pending', 'quarantined'].includes(String(instance.state || '').toLowerCase()) || (instance.provider === 'colab' && instance.verified !== true && instance.runtime_verified !== true);
}
function safeAssetUrl(url?: string): string | null {
  if (!url) return null;
  if (url.startsWith('/') && !url.startsWith('//')) return url;
  if (typeof window === 'undefined') return null;
  try { const parsed = new URL(url, window.location.origin); return parsed.origin === window.location.origin ? parsed.pathname + parsed.search : null; } catch { return null; }
}
function jobLabel(job: InstanceHistoryJob): string { return String(job.workflow_key || job.job_id || job.id || '历史任务'); }
function jobTiming(job: InstanceHistoryJob): Array<[string, string, string]> {
  const details = job.timing_details || {};
  const rows = new Map<string, [string, string, string]>(
    Object.entries(job.timings || {}).map(([name, seconds]) => [name, [name, jobStageLabel(name), formatDuration(seconds)]]),
  );
  for (const [name, detail] of Object.entries(details)) {
    if (!detail || typeof detail !== 'object') continue;
    const timing = detail as TimingDetail;
    if (timing.status === 'in_progress' || (typeof timing.seconds === 'number' && Number.isFinite(timing.seconds) && timing.seconds >= 0)) {
      rows.set(name, [name, jobStageLabel(name), formatTiming(timing)]);
    }
  }
  return [...rows.values()].map(([name, label, value]) => [name, label, timingRows(job).find((row) => row.key === name)?.value || value]);
}

const defaultWorkbenchApi: WorkbenchApi = {
  getInstanceWorkbench: async (id) => getWorkbench(id) as Promise<InstanceWorkbenchState>,
  getInstanceHistory: async (id) => historyPayload(await getInstanceHistory(id)),
  workbenchAction: (id, action, params, key) => workbenchAction(id, action as Parameters<typeof workbenchAction>[1], params, key),
};

export function InstanceWorkbench({ instance, api, onBack, onQueued }: InstanceWorkbenchProps) {
  const workbenchApi = useMemo(() => ({ ...defaultWorkbenchApi, ...api }), [api]);
  const [data, setData] = useState<InstanceWorkbenchState | null>(null);
  const [history, setHistory] = useState<InstanceHistory | null>(null);
  const [tab, setTab] = useState<WorkbenchTab>('parameters');
  const [workflow, setWorkflow] = useState('');
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [previewWorkflow, setPreviewWorkflow] = useState<ReviewedWorkflow | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [status, setStatus] = useState('');
  const [error, setError] = useState('');
  const [historyError, setHistoryError] = useState('');
  const [historyLoading, setHistoryLoading] = useState(false);
  const keyRef = useRef<Record<string, string>>({});
  const requestGeneration = useRef(0);
  const id = instanceId(instance);

  useEffect(() => {
    const generation = ++requestGeneration.current;
    let active = true;
    setData(null); setHistory(null); setWorkflow(''); setValues({}); setPreviewWorkflow(null); setBusy(null);
    setStatus(''); setError(''); setHistoryError(''); setHistoryLoading(Boolean(id));
    if (!id) { setHistoryLoading(false); return () => { active = false; }; }

    const current = () => active && generation === requestGeneration.current;
    const loadLive = async (silent = false) => {
      try {
        const next = recordPayload(await workbenchApi.getInstanceWorkbench(id));
        if (!current()) return;
        setData(next);
        setWorkflow((old) => old || (next.allowed_workflows?.[0] ? workflowIdentity(next.allowed_workflows[0]) : ''));
        setValues((old) => Object.keys(old).length ? old : { ...(next.values || {}) });
        if (!silent) setStatus('工作台状态已刷新。');
      } catch (cause) {
        if (current() && !silent) setError(apiMessage(cause));
      }
    };
    const loadHistory = async () => {
      try {
        const next = historyPayload(await workbenchApi.getInstanceHistory(id));
        if (current()) setHistory(next);
      } catch (cause) {
        if (current()) setHistoryError(apiMessage(cause));
      } finally {
        if (current()) setHistoryLoading(false);
      }
    };
    void loadLive();
    void loadHistory();
    const timer = window.setInterval(() => { void loadLive(true); void loadHistory(); }, 8000);
    return () => { active = false; window.clearInterval(timer); };
  }, [id, workbenchApi]);

  const workflows = data?.allowed_workflows || [];
  const selected = workflows.find((item) => workflowIdentity(item) === workflow);
  const parameters = selected?.parameters || data?.parameters || [];
  const readOnly = historyOnly(instance, data || undefined);
  const canQueue = !readOnly && data?.capabilities?.includes('queue_reviewed_workflow') === true && Boolean(selected);
  const canSetParams = !readOnly && data?.capabilities?.includes('set_reviewed_params') === true && Boolean(selected);

  function keyFor(action: string) { return keyRef.current[action] ||= newKey(`workbench-${action}`); }
  function clearKey(action: string) { delete keyRef.current[action]; }
  async function action(actionName: 'set_reviewed_params' | 'queue_reviewed_workflow', params: Record<string, unknown>, success: string, after?: (result: unknown) => void) {
    if (busy || !selected) return;
    const generation = requestGeneration.current;
    const current = () => generation === requestGeneration.current;
    setBusy(actionName); setError(''); setStatus('正在请求受控工作台操作…');
    try {
      const result = await workbenchApi.workbenchAction(id, actionName, params, keyFor(actionName));
      if (!current()) return;
      clearKey(actionName); setStatus(success); after?.(result);
    } catch (cause) {
      if (current()) { setError(apiMessage(cause)); setStatus('操作未确认；再次重试会复用同一幂等键。'); }
    } finally {
      if (current()) setBusy(null);
    }
  }
  const selectWorkflow = (value: string) => {
    setWorkflow(value); setPreviewWorkflow(null);
    const next = workflows.find((item) => workflowIdentity(item) === value);
    setValues({ ...(next?.draft?.values || {}) });
  };
  const saveParameters = () => { if (selected && canSetParams) void action('set_reviewed_params', { workflow_key: selected.workflow_key, workflow_version: selected.workflow_version, params: values }, '受审查参数已保存。'); };
  const queueWorkflow = () => {
    if (!selected || !canQueue) return;
    const jobKey = keyFor('queue_job');
    void action('queue_reviewed_workflow', { workflow_key: selected.workflow_key, workflow_version: selected.workflow_version, job_key: jobKey, job_input: { project_id: instance.project_id, model_id: selected.model_id || 'unknown', params: values, asset_ids: [], execution_policy: { provider: instance.provider || 'mock', max_runtime_minutes: 10, allocation: 'existing', account_id: instance.account_id || null, instance_id: id, lifecycle: 'save' } } }, '已提交受审查工作流到队列。', onQueued);
  };
  const loadGraphPreview = useCallback(async (): Promise<WorkflowGraphPreviewData> => {
    if (!previewWorkflow) throw new Error('未选择工作流');
    const result = recordPayload(await workbenchApi.workbenchAction(id, 'get_reviewed_graph', { workflow_key: previewWorkflow.workflow_key, workflow_version: previewWorkflow.workflow_version }, keyFor('get_reviewed_graph')));
    const graph = record(result);
    clearKey('get_reviewed_graph');
    return { workflow_key: previewWorkflow.workflow_key, workflow_version: previewWorkflow.workflow_version, model_id: previewWorkflow.model_id, description: previewWorkflow.description, reviewed: true, published: true, ui_graph: graph.ui_graph, api_graph: graph.api_graph || graph.graph, parameters: (previewWorkflow.parameters || []) as unknown as Array<Record<string, unknown>> };
  }, [id, previewWorkflow, workbenchApi]);

  const historyJobs = history?.jobs || [];
  return <section className={styles.workbench} aria-label="受控实例工作台">
    <header className={styles.workbenchHeader}><div><p className={styles.eyebrow}>受控工作台</p><h2>{instance.instance_label || instance.label || id}</h2><p className={styles.muted}>{gpuLabel(instance)} · {instance.state || '状态未知'} · 不暴露 Runtime 凭据</p></div>{onBack && <button type="button" className={styles.buttonSecondary} onClick={onBack}>返回实例</button>}</header>
    {status && <p className={styles.status} role="status">{status}</p>}{error && <p className={styles.error} role="alert">{error}</p>}
    {readOnly && <p className={styles.readOnlyNotice}>此实例处于历史或未验证状态，工作台只读，不会发送真实操作。</p>}
    <div className={styles.healthLine}><span className={`${styles.stateBadge} ${data?.comfy?.verified ? styles.good : ''}`}>Comfy：{data?.comfy?.available ? (data.comfy.verified ? '已验证' : '可用但未验证') : '未确认'}</span><span className={styles.stateBadge}>允许动作：{data?.capabilities?.length || 0}</span></div>
    <div className={styles.tabRow} role="tablist" aria-label="工作台内容">
      {([['parameters', '参数'], ['artifacts', '产物'], ['logs', '日志'], ['history', '历史日志']] as const).map(([key, label]) => <button type="button" role="tab" aria-selected={tab === key} className={styles.tabButton} key={key} onClick={() => setTab(key)}>{label}</button>)}
    </div>
    {tab === 'history' ? <HistoryPanel jobs={historyJobs} loading={historyLoading} error={historyError} offline={history?.offline === true} /> : !data ? <div className={styles.empty}><strong>正在读取受审查工作台…</strong><span>实时工作台不可用时，仍可打开「历史日志」查看本地持久化记录。</span></div> : <>
      {tab === 'parameters' && <div className={styles.workbenchPanel}>
        <div className={styles.field}><label htmlFor="reviewed-workflow">受审查工作流</label><select id="reviewed-workflow" value={workflow} onChange={(event) => selectWorkflow(event.target.value)} disabled={Boolean(busy)}><option value="">请选择已审核工作流</option>{workflows.map((item) => <option key={workflowIdentity(item)} value={workflowIdentity(item)}>{item.workflow_key} · v{item.workflow_version}{item.mock ? ' · Mock' : ''}</option>)}</select></div>
        {selected?.description && <p className={styles.muted}>{selected.description}</p>}
        <div className={styles.fieldList}>{parameters.map((parameter, index) => { const key = String(parameter.key || parameter.id || parameter.name || `parameter_${index + 1}`); const current = values[key] ?? parameter.default ?? ''; const editable = canSetParams && parameter.editable !== false; return <div className={styles.field} key={key}><label htmlFor={`workbench-param-${key}`}>{parameter.label || parameter.name || key}{editable ? ' · 受审查可编辑' : ' · 只读'}</label><input id={`workbench-param-${key}`} type={parameter.type === 'number' || parameter.type === 'integer' ? 'number' : 'text'} value={String(current)} disabled={!editable} min={parameter.min} max={parameter.max} step={parameter.step || (parameter.type === 'integer' ? 1 : 'any')} onChange={(event) => setValues((old) => ({ ...old, [key]: parameter.type === 'number' ? Number(event.target.value) : parameter.type === 'integer' ? Number.parseInt(event.target.value, 10) : event.target.value }))} />{parameter.description && <small>{parameter.description}</small>}</div>; })}</div>
        {!parameters.length && <p className={styles.muted}>服务端当前只返回工作流 allowlist；没有可编辑参数。</p>}
        <div className={styles.workbenchActions}><button type="button" className={styles.buttonSecondary} onClick={() => setPreviewWorkflow(selected || null)} disabled={!selected || Boolean(busy)}>读取受审查 Graph</button><button type="button" className={styles.buttonSecondary} onClick={saveParameters} disabled={!canSetParams || Boolean(busy)}>保存参数</button><button type="button" className={styles.button} onClick={queueWorkflow} disabled={!canQueue || Boolean(busy)}>{busy === 'queue_reviewed_workflow' ? '提交中…' : '提交到队列'}</button></div>
      </div>}
      {tab === 'artifacts' && <div className={styles.workbenchPanel}><div className={styles.artifactList}>{(data.artifacts || []).map((artifact, index) => { const src = safeAssetUrl(artifact.thumbnail_url); return <article className={styles.artifactItem} key={artifact.id || `${artifact.name}-${index}`}>{src ? <img className={styles.artifactThumb} src={src} alt="" /> : <div className={styles.artifactThumb} aria-hidden="true" />}<div><b>{artifact.name || artifact.filename || artifact.id || '未命名产物'}</b><small className={styles.muted}>{artifact.mime || '类型未确认'} · {artifact.status || '已记录'}</small></div></article>; })}</div>{!data.artifacts?.length && <div className={styles.empty}><strong>还没有产物</strong><span>产物来自服务端安全返回。</span></div>}</div>}
      {tab === 'logs' && <div className={styles.workbenchPanel}><div className={styles.logList}>{(data.logs || []).map((item, index) => <article className={styles.logItem} key={typeof item === 'object' && item.id ? item.id : index}><time>{typeof item === 'object' ? item.created_at || '' : ''}</time><pre>{typeof item === 'string' ? item : item.message || JSON.stringify(item)}</pre></article>)}</div>{!data.logs?.length && <div className={styles.empty}><strong>暂无安全日志</strong><span>仅显示服务端 allowlist 返回的脱敏日志。</span></div>}</div>}
    </>}
    {previewWorkflow && <WorkflowGraphPreview workflow={previewWorkflow} loadPreview={loadGraphPreview} onClose={() => setPreviewWorkflow(null)} />}
  </section>;
}

function HistoryPanel({ jobs, loading, error, offline }: { jobs: InstanceHistoryJob[]; loading: boolean; error: string; offline: boolean }) {
  return <div className={styles.workbenchPanel} aria-label="历史日志">
    <div className={styles.cardTop}><div><h3>历史日志</h3><p className={styles.muted}>{offline ? '本地持久化记录 · 不连接运行时，不执行远程操作。' : '实例历史记录'}</p></div><span className={styles.stateBadge}>{jobs.length} 个任务</span></div>
    {loading && <p className={styles.status} role="status">正在读取本地历史…</p>}
    {error && <p className={styles.error} role="alert">历史读取失败：{error}</p>}
    {!loading && !error && !jobs.length && <div className={styles.empty}><strong>暂无历史任务</strong><span>实时工作台失败不会影响此处的本地历史查询。</span></div>}
    <div className={styles.jobList}>{jobs.map((job) => <article className={styles.jobItem} key={job.id}>
      <div className={styles.cardTop}><div className={styles.minWidth}><b>{jobLabel(job)}</b><p className={styles.muted}>{job.job_id || job.id}{job.provider ? ` · ${job.provider}` : ''}</p></div><span className={`${styles.stateBadge} ${String(job.state).toLowerCase() === 'failed' ? styles.bad : ''}`}>{jobStateLabel(job.state)}</span></div>
      {job.error && <p className={styles.error} role="alert">{job.error.code ? `[${job.error.code}] ` : ''}{job.error.message || '任务失败'}</p>}
      {!!jobTiming(job).length && <div className={styles.detailGrid}><strong>耗时</strong>{jobTiming(job).map(([name, label, value]) => <span key={name}>{label}：{value}</span>)}</div>}
      {!!job.logs?.length && <div className={styles.logList}><h4>日志</h4>{job.logs.map((log, index) => <article className={styles.logItem} key={log.id || index}><time>{log.created_at || ''}</time><pre>{log.message || (log.state ? jobStateLabel(log.state) : '')}</pre></article>)}</div>}
      {!!job.artifacts?.length && <div className={styles.artifactList}><h4>产物</h4>{job.artifacts.map((artifact, index) => { const src = safeAssetUrl(artifact.thumbnail_url); return <article className={styles.artifactItem} key={artifact.id || index}>{src ? <img className={styles.artifactThumb} src={src} alt="" /> : <div className={styles.artifactThumb} aria-hidden="true" />}<div><b>{artifact.name || artifact.filename || artifact.id || '未命名产物'}</b><small className={styles.muted}>{artifact.mime || '类型未确认'} · {artifact.status || '已记录'}</small></div></article>; })}</div>}
    </article>)}</div>
  </div>;
}

export { defaultWorkbenchApi };
