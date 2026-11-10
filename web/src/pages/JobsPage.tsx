import { useState, useMemo } from 'react';
import { Zap, Search, RefreshCw, Image as ImageIcon, Film, Loader, ChevronRight, Square, RotateCcw } from 'lucide-react';
import { type GpuJob } from '../lib/gpu-api';
import styles from '../gpu.module.css';
import { formatDateTime, jobSize, progressPercent, readableState, terminalStates } from '../lib/ui-helpers';
import { jobStageLabel } from '../lib/job-stage-rows';
import { canRetainJobInstance, gpuLabel } from '../lib/gpu-label';

const RUNNING = ['running', 'uploading', 'saving', 'active', 'started', 'creating', 'reserved', 'validating', 'cleanup_pending'];
const WAITING = ['queued', 'pending', 'preparing'];
const DONE = ['completed', 'succeeded'];
const FAILED = ['failed', 'failed_final', 'cancelled', 'cancelled_local', 'blocked'];

type Filter = 'all' | 'running' | 'completed' | string;

export function TasksPanel({
  jobs, error, refreshing, onRefresh, onCancel, onRetry, onDetail, onRetain, onInstance,
}: {
  jobs: GpuJob[];
  error: string;
  refreshing: boolean;
  onRefresh: () => void;
  onCancel: (id: string) => void;
  onRetry: (id: string) => void;
  onDetail: (job: GpuJob) => void;
  onRetain?: (job: GpuJob) => void;
  onInstance?: (job: GpuJob) => void;
}) {
  const [filter, setFilter] = useState<Filter>('all');
  const [query, setQuery] = useState('');
  const [limit, setLimit] = useState(30);

  const stateOf = (job: GpuJob) => String(job.state || 'queued').toLowerCase();
  const isActive = (job: GpuJob) => RUNNING.includes(stateOf(job)) || WAITING.includes(stateOf(job));
  const uniqueJobs = useMemo(() => {
    const seen = new Set<string>();
    return jobs.filter((job, index) => {
      const key = String(job.id || job.job_id || `job-${index}`);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }, [jobs]);

  const counts = useMemo(() => ({
    running: uniqueJobs.filter(isActive).length,
    completed: uniqueJobs.filter((j) => DONE.includes(String(j.state || '').toLowerCase())).length,
  }), [uniqueJobs]);

  const filtered = uniqueJobs.filter((job) => {
    const s = stateOf(job);
    if (filter === 'running' && !isActive(job)) return false;
    if (filter === 'completed' && !DONE.includes(s)) return false;
    if (filter !== 'all' && filter !== 'running' && filter !== 'completed' && job.state !== filter) return false;
    return `${job.workflow_key || ''} ${job.id}`.toLowerCase().includes(query.toLowerCase());
  });

  const chip = (value: Filter, label: string) => (
    <button type="button" className={styles.chip} data-active={filter === value} onClick={() => setFilter(value)}>{label}</button>
  );

  return (
    <div className={styles.stack}>
      <div className={styles.pageTitle}>
        <h2><Zap size={20} strokeWidth={1.6} aria-hidden />任务队列</h2>
        <button className="button secondary small" type="button" onClick={onRefresh}>
          <RefreshCw size={14} strokeWidth={1.8} aria-hidden className={refreshing ? styles.spin : undefined} />{refreshing ? '刷新中' : '刷新'}
        </button>
      </div>

      {error && <p className="status error" role="alert">{error}</p>}

      <label className={styles.searchBox}>
        <Search size={17} strokeWidth={1.6} aria-hidden />
        <input aria-label="搜索任务" placeholder="搜索任务（名称、编号）" value={query} onChange={(e) => { setQuery(e.target.value); setLimit(30); }} />
      </label>

      <div className={styles.filterBar}>
        <div className={styles.chipRow}>
          {chip('all', `全部 ${uniqueJobs.length}`)}
          {chip('running', `进行中 ${counts.running}`)}
          {chip('completed', `已完成 ${counts.completed}`)}
        </div>
        <select aria-label="按状态筛选" className={styles.miniSelect} value={['all', 'running', 'completed'].includes(filter) ? '' : filter} onChange={(e) => setFilter(e.target.value || 'all')}>
          <option value="">更多状态</option>
          {[...new Set(uniqueJobs.map((job) => job.state || 'queued'))].map((state) => <option value={state} key={state}>{readableState(state)}</option>)}
        </select>
      </div>

      {counts.running > 0 && filter !== 'running' && (
        <button type="button" className={styles.banner} onClick={() => setFilter('running')}>
          <Loader size={16} strokeWidth={1.6} aria-hidden className={styles.spin} />
          <span><strong>{counts.running} 个任务</strong>正在排队或运行</span>
          <ChevronRight size={16} strokeWidth={1.6} aria-hidden />
        </button>
      )}

      {filtered.length ? (
        <div className={styles.list}>
          {filtered.slice(0, limit).map((job, index) => {
            const jobKey = String(job.id || job.job_id || `job-${index}`);
            const s = stateOf(job);
            const pct = progressPercent(job.progress);
            const done = DONE.includes(s);
            const running = RUNNING.includes(s);
            const failed = FAILED.includes(s);
            const waiting = WAITING.includes(s);
            const video = /i2v|video|ref2v|t2v/i.test(job.workflow_key || '');
            const thumb = done && job.output_asset_ids?.[0] && !video ? `/api/assets/${job.output_asset_ids[0]}/thumbnail` : '';
            const size = jobSize(job);
            const err = job.error_message || job.error;
            const instanceId = String(job.instance_id || '');
            const accountSnapshot = String(job.account_alias_snapshot || job.account_id || '');
            const machineInfo = [
              accountSnapshot ? `账号 ${accountSnapshot}` : '',
              String(job.instance_label || instanceId.slice(0, 8)),
              gpuLabel(job),
              instanceId ? readableState(String(job.instance_state || '未知')) : terminalStates.has(s) ? '未分配机器' : '等待分配机器',
            ].filter(Boolean).join(' · ');
            const liveInstance = canRetainJobInstance(job);
            const retained = liveInstance && Boolean(job.retained_until || job.instance_state === 'retained_idle');

            return (
              <article className={styles.jobCard} key={jobKey}>
                <div className={styles.jobTop}>
                  <div className={styles.jobThumb}>
                    {thumb ? <img src={thumb} alt="" loading="lazy" /> : video ? <Film size={22} strokeWidth={1.4} aria-hidden /> : <ImageIcon size={22} strokeWidth={1.4} aria-hidden />}
                  </div>
                  <div className={styles.jobInfo}>
                    <div className={styles.jobTitleRow}>
                      <strong title={job.workflow_key || job.id}>{job.workflow_key || `任务 ${job.id.slice(0, 8)}`}</strong>
                      <span className={`pill ${done ? 'solid' : failed ? 'danger' : ''}`}>{readableState(job.state)}</span>
                    </div>
                    <p className="muted small">
                      {[video ? '视频' : '图片', size, job.provider === 'mock' ? '模拟' : job.provider === 'colab' ? 'Colab' : ''].filter(Boolean).join(' · ')}
                    </p>
                    <p className="muted small">创建于 {formatDateTime(job.created_at)}</p>
                    {instanceId ? <button type="button" className={styles.linkButton} onClick={() => onInstance?.(job)}>{machineInfo}</button>
                      : <p className="muted small">{machineInfo}</p>}
                  </div>
                </div>

                {(running || waiting) && (
                  <div className={styles.progressBlock}>
                    <div className={styles.progressText}>
                      <span>{running ? (job.stage ? jobStageLabel(job.stage) : pct !== null ? `生成中 ${pct}%` : '生成中…') : '排队等待中'}</span>
                    </div>
                    <div className={styles.progress} data-indeterminate={pct === null}><span style={pct !== null ? { width: `${pct}%` } : undefined} /></div>
                  </div>
                )}

                {err && <p className="status error" role="alert">{err}</p>}

                <div className={styles.cardActions}>
                  <button className="button small" style={{ flex: 1 }} type="button" onClick={() => onDetail(job)}>查看详情</button>
                  {liveInstance && !retained && onRetain && <button className="button secondary small" type="button" onClick={() => onRetain(job)}>保留这台机器</button>}
                  {retained && <span className="pill">已保留{job.retained_until ? `至 ${formatDateTime(String(job.retained_until))}` : ''}</span>}
                  {!terminalStates.has(s) ? (
                    <button className="button secondary small" style={{ flex: 1 }} type="button" onClick={() => onCancel(job.id)}>
                      <Square size={13} strokeWidth={1.8} aria-hidden />取消
                    </button>
                  ) : (
                    <button className="button secondary small" style={{ flex: 1 }} type="button" onClick={() => onRetry(job.id)}>
                      <RotateCcw size={13} strokeWidth={1.8} aria-hidden />{failed ? '重试' : '再来一次'}
                    </button>
                  )}
                </div>
              </article>
            );
          })}
        </div>
      ) : (
        <div className="empty">
          <strong>还没有任务</strong>
          <span>在「创作」里点「立即生成」，任务会出现在这里。</span>
        </div>
      )}

      {filtered.length > limit && (
        <button className="button secondary block" type="button" onClick={() => setLimit(limit + 30)}>加载更多任务</button>
      )}
    </div>
  );
}
