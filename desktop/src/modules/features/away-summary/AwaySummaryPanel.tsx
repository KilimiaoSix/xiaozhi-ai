import { useCallback, useEffect, useRef, useState } from 'react';

import { groupAwayEvents, type AwayBucketGroup } from './awayBuckets';
import { awaySummaryDesktopGateway } from './services/awaySummaryDesktopGateway';
import type { AwaySummaryResult } from './types';

// 台账只在事件推送与返岗播报时变动，比告警列表安静得多，5 秒足够；
// 更密的轮询只是白白占着 Server 的事件循环。
const POLL_INTERVAL_MS = 5000;

const clockOf = (iso: string): string => {
  if (!iso || iso.length < 16) return '—';
  return iso.slice(11, 16);
};

type PanelState =
  | { kind: 'loading' }
  | { kind: 'offline'; message: string }
  | { kind: 'ready'; summary: AwaySummaryResult; groups: AwayBucketGroup[] };

/**
 * 返岗汇总面板：`GET /xiaozhi/away/summary` 的全量视图。
 *
 * 机器人回来只念三句话，这里把剩下的全列出来，分桶顺序与播报优先级一致。
 * **只读**——面板没有任何「标记已读」的动作，清账由服务端在真的播报之后做，
 * 桌面端插一脚就会把主人还没听到的留言划掉。
 */
export function AwaySummaryPanel() {
  const [state, setState] = useState<PanelState>({ kind: 'loading' });
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const activeRef = useRef(true);
  // HTTP 超时 5s 与轮询间隔同量级，在途请求会重叠。没有序号的话，
  // 一个迟到的旧快照会把新状态覆盖掉（同 IncidentPanel 的防乱序闸门）。
  const requestSeqRef = useRef(0);

  const applyState = useCallback((seq: number, next: PanelState): void => {
    if (!activeRef.current || seq !== requestSeqRef.current) return;
    setState(next);
  }, []);

  const refresh = useCallback(async (): Promise<void> => {
    const seq = ++requestSeqRef.current;
    try {
      const summary = await awaySummaryDesktopGateway.getSummary();
      applyState(seq, {
        kind: 'ready',
        summary,
        groups: groupAwayEvents(summary.items),
      });
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

  const summary = state.kind === 'ready' ? state.summary : null;

  return (
    <section className="away-panel" id="away-panel">
      <div className="section-heading compact">
        <div>
          <span className="section-index">AWAY LEDGER</span>
          <h2>返岗汇总</h2>
        </div>
        <div className="away-toolbar">
          {summary && (
            <span className={`away-state ${summary.away ? 'is-away' : ''}`}>
              <i />
              {summary.away
                ? `离席中 · 已 ${summary.awayMinutes} 分钟`
                : `在岗 · 上次离开 ${summary.awayMinutes} 分钟`}
            </span>
          )}
          <button
            type="button"
            className="away-refresh"
            onClick={() => void refresh()}
          >
            手动刷新
          </button>
        </div>
      </div>

      {state.kind === 'loading' && <p className="away-empty">正在连接本地 Server…</p>}
      {state.kind === 'offline' && (
        <p className="away-empty away-offline" role="alert">{state.message}</p>
      )}

      {summary && (
        <p className="away-headline">
          <strong>{summary.count}</strong> 条待汇总
          {summary.awaySince && <small>离席自 {clockOf(summary.awaySince)}</small>}
        </p>
      )}

      {/* 播报稿直接用服务端组好的那份：桌面端另拼一句会和机器人念的对不上 */}
      {summary?.speech && <p className="away-speech">{summary.speech}</p>}

      {state.kind === 'ready' && state.groups.length === 0 && (
        <p className="away-empty">离席期间没有需要汇总的事项。</p>
      )}

      {state.kind === 'ready' && state.groups.map((group) => (
        <div className="away-bucket" key={group.id}>
          <div className="away-bucket-heading">
            <span className={`away-bucket-title bucket-${group.id}`}>{group.label}</span>
            <span className="away-bucket-count">{group.items.length} 条</span>
          </div>
          <ul className="away-bucket-list">
            {group.items.map((item, index) => (
              <li className="away-event" key={`${group.id}-${item.taskKey || item.at}-${index}`}>
                <span className="away-event-at">{clockOf(item.at)}</span>
                <span className="away-event-text">{item.text}</span>
                {item.count > 1 && <span className="away-event-repeat">×{item.count}</span>}
                {item.source && <span className="away-event-source">{item.source}</span>}
              </li>
            ))}
          </ul>
        </div>
      ))}

      <p className="away-note">
        只读视图：清账由 Server 在机器人真的播报之后完成，刷新这里不会把留言标成已听。
      </p>
    </section>
  );
}
