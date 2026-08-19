import { useCallback, useEffect, useRef, useState } from 'react';

import { pomodoroDesktopGateway } from './services/pomodoroDesktopGateway';
import type { PomodoroCommand, PomodoroStatus } from './types';

const POLL_INTERVAL_MS = 2000;

const phaseLabels: Record<PomodoroStatus['phase'], string> = {
  focus: '专注',
  short_break: '短休',
  long_break: '长休',
  idle: '空闲',
};

const phaseTones: Record<PomodoroStatus['phase'], 'violet' | 'cyan' | 'amber'> = {
  focus: 'violet',
  short_break: 'cyan',
  long_break: 'amber',
  idle: 'amber',
};

const formatClock = (seconds: number): string => {
  const clamped = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(clamped / 60);
  const secs = clamped % 60;
  return `${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`;
};

type PanelState =
  | { kind: 'loading' }
  | { kind: 'offline'; message: string }
  | { kind: 'no-device' }
  | { kind: 'ready'; deviceId: string; status: PomodoroStatus; fetchedAtMs: number };

// /devices 的契约是"在线 ∪ 有活跃会话"：设备 WiFi 抖动的十几秒重连窗口里，
// 同一台设备会是 connected:false + active:true。只认 connected 会让面板退化成
// "没有设备"，倒计时凭空消失、也没法暂停/停止——所以在线设备优先，
// 否则退到仍在跑会话的那台。
const locateActiveDevice = async (): Promise<PanelState> => {
  const { devices } = await pomodoroDesktopGateway.listDevices();
  const target = devices.find((device) => device.connected)
    ?? devices.find((device) => device.active);
  if (!target) return { kind: 'no-device' };
  const status = await pomodoroDesktopGateway.getStatus(target.deviceId);
  return { kind: 'ready', deviceId: target.deviceId, status, fetchedAtMs: Date.now() };
};

// 快照之间的本地插值：轮询每 2s 才校准一次，纯靠它数字每 2 秒才跳一次，观感是
// "倒计时很慢"。与固件同一套做法——按快照锚点 + 真实流逝时间推算，而不是每秒
// 自减：窗口被 Electron 后台节流时 interval 会欠账，自减会把欠掉的秒数永久丢掉，
// 锚点推算在回到前台的下一次渲染就自动追平。走到 00:00 停住等服务端推下一相位。
const displayedRemainingS = (
  status: PomodoroStatus,
  fetchedAtMs: number,
  nowMs: number,
): number => {
  if (!status.active || status.paused || status.phase === 'idle') return status.remainingS;
  const elapsedS = Math.floor(Math.max(0, nowMs - fetchedAtMs) / 1000);
  return Math.max(0, status.remainingS - elapsedS);
};

export function PomodoroPanel() {
  const [state, setState] = useState<PanelState>({ kind: 'loading' });
  const [busy, setBusy] = useState(false);
  const [nowMs, setNowMs] = useState(() => Date.now());
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const activeRef = useRef(true);
  // HTTP 超时 5s 比轮询间隔 2s 长，在途请求必然重叠。没有序号的话，
  // 一个迟到的旧快照会把新状态覆盖掉，表现是倒计时忽然往回跳。
  const requestSeqRef = useRef(0);

  const applyState = useCallback((seq: number, next: PanelState): void => {
    if (!activeRef.current || seq !== requestSeqRef.current) return;
    setState(next);
  }, []);

  // 每次轮询都重新走 listDevices，这样设备上线/离线或换设备时能自然被发现，
  // 不需要额外维护"设备是否变化"的判断逻辑。
  const refresh = useCallback(async (): Promise<void> => {
    const seq = ++requestSeqRef.current;
    try {
      applyState(seq, await locateActiveDevice());
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

  // 倒计时跑动时才起 1Hz 本地重渲；空闲/暂停/离线面板不需要每秒醒一次
  const ticking = state.kind === 'ready' && state.status.active
    && !state.status.paused && state.status.phase !== 'idle';

  useEffect(() => {
    if (!ticking) return undefined;
    const id = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(id);
  }, [ticking]);

  const runCommand = async (deviceId: string, command: PomodoroCommand): Promise<void> => {
    setBusy(true);
    try {
      await pomodoroDesktopGateway.sendCommand(deviceId, command);
      await refresh();
    } catch (error) {
      // 命令失败过去只会变成 unhandled rejection，界面上什么都不发生。
      // 把失败摊到面板上，同时推进序号作废在途的旧轮询，等下一轮 poll 自恢复。
      const seq = ++requestSeqRef.current;
      applyState(seq, {
        kind: 'offline',
        message: error instanceof Error ? error.message : '番茄钟命令下发失败',
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="pomodoro-panel" id="pomodoro-panel">
      <div className="section-heading compact">
        <div>
          <span className="section-index">POMODORO</span>
          <h2>番茄钟</h2>
        </div>
        <span className={`live-dot ${state.kind === 'ready' ? '' : 'is-idle'}`} aria-label="番茄钟状态" />
      </div>

      {state.kind === 'loading' && <p className="pomodoro-empty">正在连接本地 Server…</p>}
      {state.kind === 'offline' && (
        <p className="pomodoro-empty pomodoro-offline" role="alert">{state.message}</p>
      )}
      {state.kind === 'no-device' && <p className="pomodoro-empty">没有已连接的小智设备。</p>}

      {state.kind === 'ready' && (
        <div className="pomodoro-body">
          <div className={`pomodoro-clock tone-${phaseTones[state.status.phase]}`}>
            <span className="pomodoro-phase">
              {phaseLabels[state.status.phase]}
              {state.status.paused ? ' · 已暂停' : ''}
            </span>
            <strong>{formatClock(displayedRemainingS(state.status, state.fetchedAtMs, nowMs))}</strong>
            <small>第 {state.status.round} / {state.status.totalRounds} 轮</small>
          </div>
          <div className="pomodoro-actions">
            {state.status.active ? (
              <>
                {/* 设备离线时这些按钮照样可用：服务端对离线设备的命令一样生效，只是跳过推送 */}
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void runCommand(state.deviceId, state.status.paused ? 'resume' : 'pause')}
                >
                  {state.status.paused ? '继续' : '暂停'}
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void runCommand(state.deviceId, 'skip')}
                >
                  跳过
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void runCommand(state.deviceId, 'stop')}
                >
                  停止
                </button>
              </>
            ) : (
              // 空闲时三个按钮全灰会让面板没有任何入口，start 只能从功能卡片触发
              <button
                type="button"
                disabled={busy || !state.status.connected}
                onClick={() => void runCommand(state.deviceId, 'start')}
              >
                开始专注
              </button>
            )}
          </div>
        </div>
      )}

      {state.kind === 'ready' && !state.status.connected && state.status.active && (
        <p className="pomodoro-empty pomodoro-device-offline" role="status">
          设备离线，会话仍在运行。暂停/停止照常生效，设备重连后同步。
        </p>
      )}
    </section>
  );
}
