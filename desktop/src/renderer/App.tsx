import { useEffect, useMemo, useState } from 'react';

import { featureCatalog, featureRegistry } from '../modules/features';
import type {
  AgentSource,
  AgentTaskSnapshot,
} from '../modules/features/coding-agent-status/agent-hooks/contracts';
import type { AgentHooksSnapshot } from '../modules/features/coding-agent-status/agent-hooks/runtime';
import { featureRuntimeContext, serverGateway } from '../modules/runtime';
import type { FeatureDefinition } from '../shared/features';

interface TimelineEvent {
  id: number;
  time: string;
  title: string;
  detail: string;
  tone: FeatureDefinition['tone'];
}

const initialEvents: TimelineEvent[] = [
  {
    id: 1,
    time: '现在',
    title: '桌面控制台已启动',
    detail: '八项演示能力已注册，等待逐项接入。',
    tone: 'cyan',
  },
  {
    id: 2,
    time: '待配置',
    title: 'Server 地址尚未保存',
    detail: '输入局域网地址后即可开始连接调试。',
    tone: 'amber',
  },
];

const timeLabel = (): string =>
  new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(new Date());

const sourceLabels: Record<AgentSource, string> = {
  codex: 'Codex',
  'claude-code': 'Claude Code',
  workbuddy: 'WorkBuddy',
};

const statusLabels: Record<AgentTaskSnapshot['status'], string> = {
  running: '运行中',
  completed: '已完成',
  failed: '失败',
  needs_user: '需要你',
};

const emptyAgentSnapshot: AgentHooksSnapshot = {
  installations: [],
  primaryTask: null,
  tasks: [],
  actionIntents: [],
  updatedAt: new Date(0).toISOString(),
};

const formatTaskTime = (value: string): string =>
  new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(new Date(value));

export function App() {
  const [activeFeature, setActiveFeature] = useState(featureCatalog[0]);
  const [events, setEvents] = useState(initialEvents);
  const [serverUrl, setServerUrl] = useState('http://192.168.1.100:8003');
  const [savedUrl, setSavedUrl] = useState('');
  const [agentSnapshot, setAgentSnapshot] = useState(emptyAgentSnapshot);
  const [agentBusy, setAgentBusy] = useState<AgentSource | 'all' | null>(null);
  const [agentError, setAgentError] = useState('');

  const runtime = useMemo(() => window.xiaofei.getRuntimeInfo(), []);

  useEffect(() => {
    let active = true;
    const unsubscribe = window.xiaofei.agentHooks.onSnapshot((snapshot) => {
      if (active) setAgentSnapshot(snapshot);
    });

    void window.xiaofei.agentHooks.getSnapshot()
      .then((snapshot) => {
        if (active) setAgentSnapshot(snapshot);
        return window.xiaofei.agentHooks.detect();
      })
      .catch((error: unknown) => {
        if (active) {
          setAgentError(error instanceof Error ? error.message : '无法读取 Agent Hook 状态。');
        }
      });

    return () => {
      active = false;
      unsubscribe();
    };
  }, []);

  const pushEvent = (event: Omit<TimelineEvent, 'id' | 'time'>): void => {
    setEvents((current) => [{
      id: Date.now(),
      time: timeLabel(),
      ...event,
    }, ...current].slice(0, 5));
  };

  const installAgentHook = async (source: AgentSource): Promise<void> => {
    setAgentBusy(source);
    setAgentError('');
    try {
      const result = await window.xiaofei.agentHooks.install(source);
      pushEvent({
        title: `${sourceLabels[source]} Hook ${result.ok ? '已接入' : '接入失败'}`,
        detail: result.message,
        tone: result.ok ? 'cyan' : 'coral',
      });
      if (!result.ok) setAgentError(result.message);
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Hook 接入失败。';
      setAgentError(message);
      pushEvent({ title: `${sourceLabels[source]} Hook 接入失败`, detail: message, tone: 'coral' });
    } finally {
      setAgentBusy(null);
    }
  };

  const uninstallAgentHook = async (source: AgentSource): Promise<void> => {
    setAgentBusy(source);
    setAgentError('');
    try {
      const result = await window.xiaofei.agentHooks.uninstall(source);
      pushEvent({
        title: `${sourceLabels[source]} Hook 已移除`,
        detail: result.message,
        tone: result.ok ? 'amber' : 'coral',
      });
      if (!result.ok) setAgentError(result.message);
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Hook 移除失败。';
      setAgentError(message);
    } finally {
      setAgentBusy(null);
    }
  };

  const installAllAgentHooks = async (): Promise<void> => {
    setAgentBusy('all');
    setAgentError('');
    try {
      const results = await window.xiaofei.agentHooks.installAll();
      const succeeded = results.filter((result) => result.ok).length;
      const detail = results.map((result) => `${sourceLabels[result.source]}：${result.message}`).join('；');
      pushEvent({
        title: `Agent Hook 接入完成（${succeeded}/${results.length}）`,
        detail,
        tone: succeeded === results.length ? 'cyan' : 'amber',
      });
      if (succeeded !== results.length) setAgentError(detail);
    } catch (error) {
      const message = error instanceof Error ? error.message : '批量接入 Hook 失败。';
      setAgentError(message);
      pushEvent({ title: 'Agent Hook 接入失败', detail: message, tone: 'coral' });
    } finally {
      setAgentBusy(null);
    }
  };

  const focusAgentMonitor = (): void => {
    document.querySelector('#agent-monitor')?.scrollIntoView({ behavior: 'smooth' });
  };

  const triggerFeature = async (feature: FeatureDefinition): Promise<void> => {
    setActiveFeature(feature);

    try {
      const result = await featureRegistry.execute(feature.id, featureRuntimeContext);
      setEvents((current) => [
        {
          id: Date.now(),
          time: timeLabel(),
          title: result.title,
          detail: result.detail,
          tone: result.tone,
        },
        ...current,
      ].slice(0, 5));
    } catch (error) {
      const failureEvent: TimelineEvent = {
        id: Date.now(),
        time: timeLabel(),
        title: `${feature.title}执行失败`,
        detail: error instanceof Error ? error.message : '模块返回了未知错误。',
        tone: 'coral',
      };
      setEvents((current) => [failureEvent, ...current].slice(0, 5));
    }
  };

  const saveServerUrl = (): void => {
    const normalizedUrl = serverGateway.setBaseUrl(serverUrl);
    const eventTone: TimelineEvent['tone'] = normalizedUrl ? 'cyan' : 'amber';
    setSavedUrl(normalizedUrl);
    setEvents((current) => [
      {
        id: Date.now(),
        time: timeLabel(),
        title: 'Server 地址已保存',
        detail: normalizedUrl || '地址已清空，连接保持关闭。',
        tone: eventTone,
      },
      ...current,
    ].slice(0, 5));
  };

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand" aria-label="小飞桌面机器人">
          <span className="brand-mark" aria-hidden="true">XF</span>
          <div>
            <strong>小飞</strong>
            <span>DESKBOT CONSOLE</span>
          </div>
        </div>
        <div className="topbar-status">
          <span className="build-label">LAB BUILD 0.1.0</span>
          <span className="runtime-label">Electron {runtime.versions.electron}</span>
          <span className={`status-pill ${agentSnapshot.installations.some((item) => item.installed) ? 'is-live' : 'is-idle'}`}>
            <i />{agentSnapshot.installations.some((item) => item.installed) ? 'Agent 监控中' : '系统待连接'}
          </span>
        </div>
      </header>

      <main>
        <section className="hero-grid">
          <div className="hero-copy">
            <p className="eyebrow">WORKSTATION PRESENCE / 08:42</p>
            <h1>今天，小飞<br />在工位上。</h1>
            <p className="hero-summary">
              一个认识你、理解工作上下文，也能替你守住工位的桌面机器人。
              现在先把每条能力链路留好位置。
            </p>

            <div className="neural-link" aria-label="系统连接链路">
              <div className="link-node">
                <span>01</span>
                <strong>Electron</strong>
                <small>本机在线</small>
              </div>
              <div className="link-line"><i /></div>
              <div className="link-node">
                <span>02</span>
                <strong>Server</strong>
                <small>{savedUrl ? '地址已配置' : '等待配置'}</small>
              </div>
              <div className="link-line is-muted"><i /></div>
              <div className="link-node is-muted">
                <span>03</span>
                <strong>Deskbot</strong>
                <small>WebSocket 离线</small>
              </div>
            </div>
          </div>

          <aside className="connection-panel">
            <div className="panel-heading">
              <div>
                <span className="panel-kicker">CONNECTION</span>
                <h2>连接工作台</h2>
              </div>
              <span className="signal-glyph" aria-hidden="true"><i /><i /><i /></span>
            </div>
            <label htmlFor="server-url">Server 地址</label>
            <div className="url-control">
              <input
                id="server-url"
                value={serverUrl}
                onChange={(event) => setServerUrl(event.target.value)}
                placeholder="http://192.168.1.100:8003"
              />
              <button type="button" onClick={saveServerUrl}>保存地址</button>
            </div>
            <dl className="connection-meta">
              <div><dt>控制链路</dt><dd>Electron → HTTP → Server</dd></div>
              <div><dt>机器人链路</dt><dd>Server → WebSocket → ESP32</dd></div>
              <div><dt>当前模式</dt><dd className="mock-value">MOCK / SAFE</dd></div>
            </dl>
            <p className="panel-note">
              此阶段不会发送真实请求。接口与鉴权将在 Server 适配器中接入。
            </p>
          </aside>
        </section>

        <section className="agent-monitor" id="agent-monitor">
          <div className="section-heading agent-heading">
            <div>
              <span className="section-index">LOCAL AGENT HOOKS</span>
              <h2>AI Agent 任务监控</h2>
            </div>
            <div className="agent-heading-actions">
              <span>{agentSnapshot.installations.filter((item) => item.installed).length} / 3 已接入</span>
              <button
                className="primary-action"
                type="button"
                disabled={agentBusy !== null}
                onClick={() => void installAllAgentHooks()}
              >
                {agentBusy === 'all' ? '正在配置…' : '自动发现并接入'}
              </button>
            </div>
          </div>

          {agentError && <p className="agent-error" role="alert">{agentError}</p>}

          <div className="agent-source-grid">
            {(['codex', 'claude-code', 'workbuddy'] as const).map((source) => {
              const detection = agentSnapshot.installations.find((item) => item.source === source);
              const installed = detection?.installed ?? false;
              return (
                <article className={`agent-source-card ${installed ? 'is-installed' : ''}`} key={source}>
                  <div>
                    <span className="agent-source-code">{source.toUpperCase()}</span>
                    <span className={`agent-install-state ${installed ? 'is-installed' : ''}`}>
                      <i />{installed ? '正在监控' : detection?.available ? '可接入' : '未发现'}
                    </span>
                  </div>
                  <h3>{sourceLabels[source]}</h3>
                  <p>{detection?.message ?? '正在检查本机安装与 Hook 配置…'}</p>
                  <small title={detection?.configPath}>{detection?.configPath ?? '等待检测配置路径'}</small>
                  <button
                    type="button"
                    disabled={agentBusy !== null || (!installed && detection !== undefined && !detection.available)}
                    onClick={() => void (installed ? uninstallAgentHook(source) : installAgentHook(source))}
                  >
                    {agentBusy === source ? '处理中…' : installed ? '移除 Hook' : '接入 Hook'}
                  </button>
                </article>
              );
            })}
          </div>

          <div className="agent-task-layout">
            <article className={`primary-task ${agentSnapshot.primaryTask ? `status-${agentSnapshot.primaryTask.status}` : 'is-empty'}`}>
              <div className="task-card-heading">
                <span>CURRENT TASK</span>
                {agentSnapshot.primaryTask && (
                  <span className={`task-status status-${agentSnapshot.primaryTask.status}`}>
                    <i />{statusLabels[agentSnapshot.primaryTask.status]}
                  </span>
                )}
              </div>
              {agentSnapshot.primaryTask ? (
                <TaskContent task={agentSnapshot.primaryTask} />
              ) : (
                <div className="empty-task">
                  <strong>等待 Agent 任务</strong>
                  <p>接入 Hook 后，在 Codex、Claude Code 或 WorkBuddy 中开始任务，状态会自动出现在这里。</p>
                </div>
              )}
            </article>

            <aside className="task-queue">
              <div className="task-card-heading">
                <span>RECENT TASKS</span>
                <small>{agentSnapshot.tasks.length} 项</small>
              </div>
              {agentSnapshot.tasks.length ? (
                <ol>
                  {agentSnapshot.tasks.slice(0, 8).map((task) => (
                    <li key={task.key}>
                      <span className={`task-status status-${task.status}`}><i />{statusLabels[task.status]}</span>
                      <strong>{task.title}</strong>
                      <small>{sourceLabels[task.source]} · {formatTaskTime(task.updatedAt)}</small>
                    </li>
                  ))}
                </ol>
              ) : (
                <p className="task-queue-empty">暂无任务记录</p>
              )}
            </aside>
          </div>

          {agentSnapshot.actionIntents.length > 0 && (
            <div className="robot-intent">
              <span>ROBOT FEEDBACK</span>
              <strong>{agentSnapshot.actionIntents.at(-1)?.action.replaceAll('_', ' ')}</strong>
              <small>已生成机器人反馈意图，等待 Server / ESP32 链路消费。</small>
            </div>
          )}
        </section>

        <section className="workspace-grid">
          <div className="capabilities">
            <div className="section-heading">
              <div>
                <span className="section-index">8 CAPABILITIES</span>
                <h2>能力占位</h2>
              </div>
              <p>每张卡片对应一个独立扩展入口</p>
            </div>

            <div className="feature-grid">
              {featureCatalog.map((feature) => (
                <article
                  className={`feature-card tone-${feature.tone} ${activeFeature.id === feature.id ? 'is-active' : ''}`}
                  key={feature.id}
                >
                  <div className="feature-topline">
                    <span className="feature-code">{feature.code}</span>
                    <span className={`placeholder-state ${feature.status === 'ready' ? 'is-ready' : ''}`}>
                      <i />{feature.status === 'ready' ? '已接入' : '待接入'}
                    </span>
                  </div>
                  <h3>{feature.title}</h3>
                  <p>{feature.summary}</p>
                  <button
                    type="button"
                    onClick={() => {
                      if (feature.id === 'coding-agent-status') focusAgentMonitor();
                      void triggerFeature(feature);
                    }}
                    aria-pressed={activeFeature.id === feature.id}
                  >
                    {feature.triggerLabel}<span aria-hidden="true">↗</span>
                  </button>
                </article>
              ))}
            </div>
          </div>

          <aside className="event-rail">
            <div className="section-heading compact">
              <div>
                <span className="section-index">EVENT STREAM</span>
                <h2>最近事件</h2>
              </div>
              <span className="live-dot" aria-label="事件流运行中" />
            </div>

            <div className="active-feature">
              <span>当前模块 / {activeFeature.code}</span>
              <strong>{activeFeature.title}</strong>
              <p>模块已注册，可从当前 Mock 实现逐步替换为真实适配器。</p>
            </div>

            <ol className="event-list">
              {events.map((event) => (
                <li key={event.id} className={`tone-${event.tone}`}>
                  <time>{event.time}</time>
                  <div>
                    <strong>{event.title}</strong>
                    <p>{event.detail}</p>
                  </div>
                </li>
              ))}
            </ol>
          </aside>
        </section>
      </main>

      <footer>
        <span>XIAOFEI SYSTEM / MACOS {runtime.platform.toUpperCase()}</span>
        <span>Agent Hook 已本机接入 / Server 与机器人链路仍为安全占位</span>
      </footer>
    </div>
  );
}

function TaskContent({ task }: { task: AgentTaskSnapshot }) {
  return (
    <div className="task-content">
      <div className="task-source-line">
        <span>{sourceLabels[task.source]}</span>
        <time>{formatTaskTime(task.updatedAt)}</time>
      </div>
      <h3>{task.title}</h3>
      {task.cwd && <p className="task-cwd" title={task.cwd}>{task.cwd}</p>}
      {task.prompt && (
        <div className="task-prompt">
          <span>完整提示词</span>
          <pre>{task.prompt}</pre>
        </div>
      )}
      {task.needsUserReason && <p className="task-attention">需要处理：{task.needsUserReason}</p>}
      {task.error && <p className="task-failure">错误：{task.error}</p>}
    </div>
  );
}
