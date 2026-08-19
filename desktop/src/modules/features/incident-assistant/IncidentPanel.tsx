import { useCallback, useEffect, useRef, useState } from 'react';

import { incidentDesktopGateway } from './services/incidentDesktopGateway';
import type { IncidentSummary } from './types';

const POLL_INTERVAL_MS = 2000;

const SEVERITY_RANK: Record<IncidentSummary['severity'], number> = {
  P0: 0,
  P1: 1,
  P2: 2,
  P3: 3,
};

const STATE_RANK: Record<IncidentSummary['state'], number> = {
  firing: 0,
  observing: 1,
  recovered: 2,
};

const STATE_LABELS: Record<IncidentSummary['state'], string> = {
  firing: '告警中',
  observing: '恢复观察',
  recovered: '已恢复',
};

const RELAY_TOOLTIP = '该来源走值班中继自有诊断闭环（飞书认领后自动触发），桌面端仅展示';

// 需求定死的展示序：先严重度（P0>P1>P2>P3），同级再按状态（firing>observing>
// recovered），最后按最近动静兜底，保证轮询间顺序稳定不跳动。
const sortIncidents = (incidents: IncidentSummary[]): IncidentSummary[] =>
  [...incidents].sort(
    (a, b) =>
      SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity]
      || STATE_RANK[a.state] - STATE_RANK[b.state]
      || (b.lastSeenAt ?? '').localeCompare(a.lastSeenAt ?? ''),
  );

const clockOf = (iso: string | null): string => {
  if (!iso || iso.length < 16) return '—';
  return iso.slice(11, 16);
};

type PanelState =
  | { kind: 'loading' }
  | { kind: 'offline'; message: string }
  | { kind: 'ready'; date: string; incidents: IncidentSummary[] };

export function IncidentPanel() {
  const [state, setState] = useState<PanelState>({ kind: 'loading' });
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [busyIds, setBusyIds] = useState<ReadonlySet<string>>(new Set());
  // 点击「触发诊断」到服务端在 list 里报出 diagnosis.state 之间的本地过渡态；
  // 以服务端返回为准：一旦 list 带出 diagnosis（running/done/failed）就摘掉
  const [pendingDiagnosis, setPendingDiagnosis] = useState<ReadonlySet<string>>(new Set());
  const [actionError, setActionError] = useState<string | null>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const activeRef = useRef(true);
  // HTTP 超时 5s 比轮询间隔 2s 长，在途请求必然重叠。没有序号的话，
  // 一个迟到的旧快照会把新状态覆盖掉（同 PomodoroPanel 的防乱序闸门）。
  const requestSeqRef = useRef(0);

  const applyState = useCallback((seq: number, next: PanelState): void => {
    if (!activeRef.current || seq !== requestSeqRef.current) return;
    setState(next);
    if (next.kind === 'ready') {
      // 服务端已经报出诊断状态的行，本地过渡态使命结束
      setPendingDiagnosis((previous) => {
        if (!previous.size) return previous;
        const remaining = new Set<string>();
        for (const id of previous) {
          const row = next.incidents.find((item) => item.id === id);
          if (row && row.diagnosis === null) remaining.add(id);
        }
        return remaining.size === previous.size ? previous : remaining;
      });
    }
  }, []);

  const refresh = useCallback(async (): Promise<void> => {
    const seq = ++requestSeqRef.current;
    try {
      const { date, incidents } = await incidentDesktopGateway.list();
      applyState(seq, { kind: 'ready', date, incidents: sortIncidents(incidents) });
    } catch (error) {
      applyState(seq, {
        kind: 'offline',
        message: error instanceof Error ? error.message : '本地 Server 当前不可用',
      });
    }
  }, [applyState]);

  useEffect(() => {
    activeRef.current = true;
    void refresh();
    intervalRef.current = setInterval(() => void refresh(), POLL_INTERVAL_MS);
    return () => {
      activeRef.current = false;
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [refresh]);

  const markBusy = (id: string, busy: boolean): void => {
    setBusyIds((previous) => {
      const next = new Set(previous);
      if (busy) next.add(id);
      else next.delete(id);
      return next;
    });
  };

  const runAck = async (incident: IncidentSummary): Promise<void> => {
    setActionError(null);
    markBusy(incident.id, true);
    try {
      await incidentDesktopGateway.ack(incident.id);
      await refresh();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : '标记已处理失败');
    } finally {
      markBusy(incident.id, false);
    }
  };

  const runDiagnose = async (incident: IncidentSummary): Promise<void> => {
    setActionError(null);
    // 先置「诊断中」再发请求：诊断要跑几分钟，按钮必须立刻给出反馈并防连点。
    // 服务端 409（已有诊断在跑）已在网关层归一成 accepted=false，同样算进行中。
    setPendingDiagnosis((previous) => new Set(previous).add(incident.id));
    markBusy(incident.id, true);
    try {
      await incidentDesktopGateway.diagnose(incident.id);
      await refresh();
    } catch (error) {
      setPendingDiagnosis((previous) => {
        const next = new Set(previous);
        next.delete(incident.id);
        return next;
      });
      setActionError(error instanceof Error ? error.message : '触发诊断失败');
    } finally {
      markBusy(incident.id, false);
    }
  };

  const diagnosisRunning = (incident: IncidentSummary): boolean =>
    incident.diagnosis?.state === 'running' || pendingDiagnosis.has(incident.id);

  const renderDiagnosis = (incident: IncidentSummary) => {
    const diagnosis = incident.diagnosis;
    if (diagnosisRunning(incident)) {
      return <p className="incident-diagnosis is-running">诊断进行中，结论会自动出现在这里…</p>;
    }
    if (!diagnosis) return null;
    if (diagnosis.state === 'done') {
      return (
        <p className="incident-diagnosis is-done">
          <strong>诊断结论</strong>
          {diagnosis.summary}
          {diagnosis.finishedAt ? <small>{clockOf(diagnosis.finishedAt)} 完成</small> : null}
        </p>
      );
    }
    if (diagnosis.state === 'failed') {
      return (
        <p className="incident-diagnosis is-failed">
          <strong>诊断失败</strong>
          {diagnosis.summary || '未知原因'}
        </p>
      );
    }
    return null;
  };

  return (
    <section className="incident-panel" id="incident-panel">
      <div className="section-heading compact">
        <div>
          <span className="section-index">INCIDENTS</span>
          <h2>告警管理</h2>
        </div>
        <div className="incident-toolbar">
          {state.kind === 'ready' && <span className="incident-date">{state.date}</span>}
          <button
            type="button"
            className="incident-refresh"
            onClick={() => void refresh()}
          >
            手动刷新
          </button>
        </div>
      </div>

      {actionError && (
        <p className="incident-action-error" role="alert">{actionError}</p>
      )}

      {state.kind === 'loading' && <p className="incident-empty">正在连接本地 Server…</p>}
      {state.kind === 'offline' && (
        <p className="incident-empty incident-offline" role="alert">{state.message}</p>
      )}
      {state.kind === 'ready' && state.incidents.length === 0 && (
        <p className="incident-empty">今天还没有告警记录。</p>
      )}

      {state.kind === 'ready' && state.incidents.length > 0 && (
        <ul className="incident-list">
          {state.incidents.map((incident) => {
            const critical = incident.state === 'firing'
              && (incident.severity === 'P0' || incident.severity === 'P1');
            const expanded = expandedId === incident.id;
            const busy = busyIds.has(incident.id);
            const running = diagnosisRunning(incident);
            const isRelay = incident.source === 'alert_relay';
            return (
              <li
                key={incident.id}
                className={`incident-row ${critical ? 'is-critical' : ''} ${expanded ? 'is-expanded' : ''}`}
              >
                <div className="incident-row-line">
                  <button
                    type="button"
                    className="incident-row-head"
                    aria-expanded={expanded}
                    onClick={() => setExpandedId(expanded ? null : incident.id)}
                  >
                    <span className={`incident-severity severity-${incident.severity.toLowerCase()}`}>
                      {incident.severity}
                    </span>
                    {incident.simulated && <span className="incident-simulated">模拟</span>}
                    {isRelay && <span className="incident-source-tag">值班中继</span>}
                    <span className="incident-service">{incident.service}</span>
                    <span className="incident-title">{incident.title}</span>
                    <span className={`incident-state state-${incident.state}`}>
                      {STATE_LABELS[incident.state]}
                    </span>
                    <span className="incident-meta">
                      ×{incident.repeatCount} · {clockOf(incident.firstSeenAt)} → {clockOf(incident.lastSeenAt)}
                    </span>
                  </button>
                  <div className="incident-actions">
                    <button
                      type="button"
                      className="incident-ack"
                      disabled={
                        busy
                        || isRelay
                        || incident.acknowledged
                        || incident.state === 'recovered'
                      }
                      title={isRelay ? RELAY_TOOLTIP : undefined}
                      onClick={() => void runAck(incident)}
                    >
                      {incident.acknowledged ? '已处理' : '标记已处理'}
                    </button>
                    <button
                      type="button"
                      className="incident-diagnose"
                      disabled={busy || isRelay || running}
                      title={isRelay ? RELAY_TOOLTIP : undefined}
                      onClick={() => void runDiagnose(incident)}
                    >
                      {running ? '诊断中…' : '触发诊断'}
                    </button>
                  </div>
                </div>

                {expanded && (
                  <div className="incident-detail">
                    {incident.message && (
                      <p className="incident-message">{incident.message}</p>
                    )}
                    {renderDiagnosis(incident)}
                    <ol className="incident-timeline">
                      {incident.timeline.map((entry, index) => (
                        <li key={`${entry.at}-${entry.event}-${index}`}>
                          <span className="incident-timeline-at">{clockOf(entry.at)}</span>
                          <span className="incident-timeline-event">{entry.event}</span>
                          <span className="incident-timeline-detail">{entry.detail}</span>
                        </li>
                      ))}
                    </ol>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
