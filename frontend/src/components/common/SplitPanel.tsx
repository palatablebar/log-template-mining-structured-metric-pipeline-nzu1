import { useCallback, useEffect, useState } from 'react';
import { templateApi } from '../../api/template';
import type {
  SplitExecuteResult,
  SplitImpactPreview,
  SplitRollbackResult,
  Template,
} from '../../types/models';
import { errorMessage } from '../../utils/errorMessage';
import { formatCount } from '../../utils/formatCount';
import { formatTime } from '../../utils/formatTime';

interface Props {
  template: Template;
  canWrite: boolean;
  onChanged: () => Promise<void> | void;
}

/**
 * 模板按采集站拆分面板：
 * 进入即拉取影响范围（只读预览）→ admin 可发起拆分 → 展示拆分前后口径对比 → 支持按快照回滚。
 * 已拆分模板重复进入会回显上次作业，重复执行后端直接 409（只生效一次）。
 */
export const SplitPanel = ({ template, canWrite, onChanged }: Props) => {
  const [preview, setPreview] = useState<SplitImpactPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [acting, setActing] = useState(false);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  const [result, setResult] = useState<SplitExecuteResult | null>(null);
  const [rollback, setRollback] = useState<SplitRollbackResult | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const data = await templateApi.splitPreview(template.id);
      setPreview(data);
    } catch (caught) {
      setError(errorMessage(caught, '影响范围加载失败'));
    } finally {
      setLoading(false);
    }
  }, [template.id]);

  useEffect(() => {
    setPreview(null);
    setResult(null);
    setRollback(null);
    setNotice('');
    setError('');
    void load();
  }, [load]);

  const execute = async () => {
    if (!window.confirm(`确认把模板 #${template.id} 按采集站拆成 ${preview?.sources.length ?? 0} 个子模板？事件、规则与告警将在单事务内迁移。`)) {
      return;
    }
    setActing(true);
    setNotice('');
    setError('');
    try {
      const data = await templateApi.splitExecute(template.id);
      setResult(data);
      setRollback(null);
      setNotice(data.message ?? '拆分完成');
      await load();
      await onChanged();
    } catch (caught) {
      setError(errorMessage(caught, '拆分失败，数据未改动'));
    } finally {
      setActing(false);
    }
  };

  const doRollback = async () => {
    if (!window.confirm('按回滚快照恢复到拆分前？子模板将删除，事件/规则/告警迁回原模板。')) {
      return;
    }
    setActing(true);
    setNotice('');
    setError('');
    try {
      const data = await templateApi.splitRollback(template.id);
      setRollback(data);
      setResult(null);
      setNotice(data.message ?? '已回滚');
      await load();
      await onChanged();
    } catch (caught) {
      setError(errorMessage(caught, '回滚失败，数据未改动'));
    } finally {
      setActing(false);
    }
  };

  if (loading && !preview) {
    return <p className="loading">正在评估拆分影响范围…</p>;
  }
  if (error && !preview) {
    return (
      <div className="banner">
        <span>{error}</span>
        <button className="btn ghost small" type="button" onClick={() => void load()}>
          重试
        </button>
      </div>
    );
  }
  if (!preview) {
    return null;
  }

  const completed = preview.already_split;
  const rolledBack = preview.job?.status === 'rolled_back';

  return (
    <div className="split-panel">
      <div className="panel-head">
        <h3>按采集站拆分</h3>
        <span className="meta">{completed ? '已拆分 · 重复执行不生效' : rolledBack ? '已回滚 · 可重新拆分' : '影响范围预览'}</span>
      </div>

      {error ? <div className="banner">{error}</div> : null}
      {notice ? <div className="banner ok">{notice}</div> : null}

      {!preview.splittable && !completed ? (
        <p className="hint">{preview.reason || '该模板暂不支持拆分'}</p>
      ) : (
        <>
          <table className="ruled split-table">
            <thead>
              <tr>
                <th>采集站</th>
                <th>服务</th>
                <th>混合事件数</th>
                <th>error/fatal</th>
                <th>覆盖桶</th>
                <th>时间范围</th>
              </tr>
            </thead>
            <tbody>
              {preview.sources.map((row) => (
                <tr key={row.source_id}>
                  <td className="mono">{row.source_name}</td>
                  <td className="mono">{row.service}</td>
                  <td className="num">{formatCount(row.event_count)}</td>
                  <td className="num">{formatCount(row.error_count)}</td>
                  <td className="num">{row.bucket_count}</td>
                  <td className="mono" style={{ fontSize: 'var(--step--1)' }}>
                    {formatTime(row.first_seen_at)} ~ {formatTime(row.last_seen_at)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="hint">
            合计 {formatCount(preview.total_events)} 条混合事件；模板上规则 {preview.rules_on_template.length} 条（将按站复制）、
            历史告警 {preview.alerts_on_template} 条（按告警桶主导采集站归属）。{preview.global_rules_note}
          </p>

          {/* 拆分后结果：刚执行完的结果优先，否则回显库里现状 */}
          {(result || completed) && (
            <div className="split-after">
              <div className="panel-head">
                <h3>拆分后口径（各站独立计数）</h3>
                {preview.job?.executed_at ? (
                  <span className="meta">执行于 {formatTime(preview.job.executed_at)}</span>
                ) : null}
              </div>
              <table className="ruled split-table">
                <thead>
                  <tr>
                    <th>采集站</th>
                    <th>子模板</th>
                    <th>迁入事件</th>
                    <th>复制规则</th>
                    <th>归属告警</th>
                  </tr>
                </thead>
                <tbody>
                  {(result
                    ? result.result.after.map((row) => ({
                        source_name: row.source_name,
                        template_id: row.template_id,
                        events: row.event_count,
                        rules: row.rule_ids.length,
                        alerts: row.alert_count,
                      }))
                    : (preview.children_now ?? []).map((child) => ({
                        source_name: child.source_name || preview.sources.find((item) => item.source_id === child.source_id)?.source_name || `#${child.source_id}`,
                        template_id: child.template_id,
                        events: child.event_count,
                        rules: child.rule_count,
                        alerts: child.alert_count,
                      }))
                  ).map((row) => (
                    <tr key={row.template_id}>
                      <td className="mono">{row.source_name}</td>
                      <td className="num">#{row.template_id}</td>
                      <td className="num">{formatCount(row.events)}</td>
                      <td className="num">{row.rules}</td>
                      <td className="num">{row.alerts}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {preview.job ? (
                <p className="hint">
                  作业 #{preview.job.id}：迁移事件 {preview.job.moved_events} · 迁移告警 {preview.job.moved_alerts} ·
                  复制规则 {preview.job.duplicated_rules}
                  {rolledBack && preview.job.rolled_back_at ? ` · 已于 ${formatTime(preview.job.rolled_back_at)} 回滚` : ''}
                </p>
              ) : null}
            </div>
          )}

          {rollback ? (
            <div className="banner info">
              <span>
                回滚完成：恢复事件 {rollback.restored_events} 条、告警 {rollback.restored_alerts} 条，删除复制规则{' '}
                {rollback.removed_rules} 条、子模板 {rollback.removed_children} 个
                {rollback.drift.length ? `；检测到 ${rollback.drift.length} 个站存在拆分后新增数据，已一并迁回原模板` : ''}。
              </span>
            </div>
          ) : null}

          {canWrite ? (
            <div className="toolbar">
              {!completed ? (
                <button className="btn primary" type="button" disabled={acting || !preview.splittable} onClick={() => void execute()}>
                  {acting ? '拆分中…' : `按 ${preview.sources.length} 个采集站拆分`}
                </button>
              ) : (
                <button className="btn danger" type="button" disabled={acting} onClick={() => void doRollback()}>
                  {acting ? '回滚中…' : '回滚本次拆分'}
                </button>
              )}
              {!canWrite ? null : <span className="hint">单事务执行，失败整体回滚；操作写入审计日志。</span>}
            </div>
          ) : (
            <p className="hint">当前角色只读，拆分/回滚由 admin 执行。</p>
          )}
        </>
      )}
    </div>
  );
};
