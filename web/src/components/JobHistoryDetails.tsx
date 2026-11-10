import { timingRows, type TimingDetail } from '../lib/job-history';
import { jobStageLabel, jobStateLabel } from '../lib/job-stage-rows';

export interface JobHistoryDetailRecord {
  state?: string;
  stage?: string | null;
  timings?: Record<string, number>;
  timing_details?: Record<string, TimingDetail>;
  logs?: Array<{ id?: string; state?: string; created_at?: string; message?: string }>;
  events?: Array<{ id?: string; state?: string; created_at?: string; message?: string }>;
}

export function JobHistoryDetails({ job }: { job: JobHistoryDetailRecord }) {
  const rows = timingRows(job);
  const logs = job.logs || job.events || [];
  return (
    <>
      <p className="muted small">
        状态：{jobStateLabel(job.state)}
        {job.stage ? ` · 当前阶段：${jobStageLabel(job.stage)}` : ''}
      </p>
      {rows.length ? (
        <details className="foldable" open>
          <summary>各阶段耗时</summary>
          <div className="foldContent">
            <dl className="kv">
              {rows.map((row) => (
                <div key={row.key} style={{ display: 'contents' }}>
                  <dt>{row.label}</dt>
                  <dd>{row.value} <span className="muted small">· {row.source}</span></dd>
                </div>
              ))}
            </dl>
            <p className="muted small">单位是秒，按服务端墙钟记录。各阶段独立计时，可能重叠，相加不是工作流发布又等待的时间。</p>
          </div>
        </details>
      ) : null}
      {logs.length ? (
        <details className="foldable" open>
          <summary>持久任务日志</summary>
          <div className="foldContent">
            {logs.map((log, index) => (
              <p key={log.id || index} className="muted small">
                {log.created_at ? `${log.created_at} · ` : ''}{log.message || (log.state ? jobStateLabel(log.state) : '状态事件')}
              </p>
            ))}
          </div>
        </details>
      ) : null}
    </>
  );
}
