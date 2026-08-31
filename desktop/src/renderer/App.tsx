import { useEffect, useMemo, useState } from 'react';
import {
  ArrowUpRight,
  Bot,
  Camera,
  PlugZap,
  RefreshCw,
} from 'lucide-react';

import { AppSidebar } from './components/AppSidebar';
import type { AppPage } from './components/AppSidebar';
import { ServerSettingsPanel } from './components/ServerSettingsPanel';

import { featureRegistry, visibleFeatureCatalog } from '../modules/features';
import { AwaySummaryPanel } from '../modules/features/away-summary';
import { CameraPage } from '../modules/features/camera-capture';
import { useCameraMonitoring } from '../modules/features/camera-capture/context/CameraMonitoringProvider';
import { PomodoroPanel } from '../modules/features/focus-mode/PomodoroPanel';
import { IncidentPanel } from '../modules/features/incident-assistant/IncidentPanel';
import type {
  AgentSource,
  AgentTaskSnapshot,
} from '../modules/features/coding-agent-status/agent-hooks/contracts';
import type { AgentHooksSnapshot } from '../modules/features/coding-agent-status/agent-hooks/runtime';
import type {
  FeishuBriefingSnapshot,
  FeishuConnectionStatus,
} from '../modules/features/feishu-briefing/contracts';
import { featureRuntimeContext } from '../modules/runtime';
import type {
  AppConfigFieldResolution,
  AppConfigResolution,
} from '../shared/appConfig';
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
    detail: `${visibleFeatureCatalog.length} 项已接入能力可用，事件流只记录真实发生的事。`,
    tone: 'cyan',
  },
  {
    id: 2,
    time: '待确认',
    title: 'Server 地址取自配置中心',
    detail: '连接工作台里能看到每项配置的生效值与来源，改完即时生效。',
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

const logAgentSnapshot = (
  phase: 'initial' | 'snapshot',
  snapshot: AgentHooksSnapshot,
): void => {
  if (!import.meta.env.DEV) return;
  console.info(`[LaunchCrush][agent-hooks:${phase}]`, {
    updatedAt: snapshot.updatedAt,
    installations: snapshot.installations.map((item) => ({
      source: item.source,
      available: item.available,
      installed: item.installed,
      requiresTrustReview: item.requiresTrustReview,
      configPath: item.configPath,
      message: item.message,
    })),
    primaryTask: snapshot.primaryTask,
    tasks: snapshot.tasks,
    actionIntents: snapshot.actionIntents,
  });
};

const formatTaskTime = (value: string): string =>
  new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(new Date(value));

const formatFeishuTime = (value: string, allDay = false): string => {
  if (allDay || /^\d{4}-\d{2}-\d{2}$/.test(value)) return '全天';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date);
};

const formatFeishuDue = (value?: string, allDay = false): string => {
  if (!value) return '未设截止时间';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return `截止 ${value}`;
  return `截止 ${new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    ...(allDay ? {} : { hour: '2-digit', minute: '2-digit', hour12: false }),
  }).format(date)}`;
};

export function App() {
  const cameraMonitoring = useCameraMonitoring();
  const [activePage, setActivePage] = useState<AppPage>('dashboard');
  const [activeFeature, setActiveFeature] = useState(visibleFeatureCatalog[0]);
  const [events, setEvents] = useState(initialEvents);
  const [serverUrl, setServerUrl] = useState<AppConfigFieldResolution | null>(null);
  const [agentSnapshot, setAgentSnapshot] = useState(emptyAgentSnapshot);
  const [agentBusy, setAgentBusy] = useState<AgentSource | 'all' | null>(null);
  const [agentError, setAgentError] = useState('');
  const [feishuStatus, setFeishuStatus] = useState<FeishuConnectionStatus | null>(null);
  const [feishuBriefing, setFeishuBriefing] = useState<FeishuBriefingSnapshot | null>(null);
  const [feishuBusy, setFeishuBusy] = useState(false);
  const [feishuError, setFeishuError] = useState('');

  const runtime = useMemo(() => window.xiaofei.getRuntimeInfo(), []);
  const activeAgentHookCount = agentSnapshot.installations.filter((item) =>
    item.installed && !item.requiresTrustReview).length;

  useEffect(() => {
    let active = true;
    const unsubscribe = window.xiaofei.agentHooks.onSnapshot((snapshot) => {
      if (active) {
        logAgentSnapshot('snapshot', snapshot);
        setAgentSnapshot(snapshot);
      }
    });

    void window.xiaofei.agentHooks.getSnapshot()
      .then((snapshot) => {
        if (active) {
          logAgentSnapshot('initial', snapshot);
          setAgentSnapshot(snapshot);
        }
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

  useEffect(() => {
    let active = true;
    void window.xiaofei.feishu.getStatus()
      .then(async (status) => {
        if (!active) return;
        setFeishuStatus(status);
        if (status.state !== 'ready') return;
        const briefing = await window.xiaofei.feishu.getBriefing();
        if (active) setFeishuBriefing(briefing);
      })
      .catch((error: unknown) => {
        if (active) {
          setFeishuError(error instanceof Error ? error.message : '无法连接飞书服务。');
        }
      });
    return () => { active = false; };
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

  const focusFeishuWorkspace = (): void => {
    document.querySelector('#feishu-workspace')?.scrollIntoView({ behavior: 'smooth' });
  };

  const checkFeishuConnection = async (): Promise<void> => {
    setFeishuBusy(true);
    setFeishuError('');
    try {
      const status = await window.xiaofei.feishu.getStatus();
      setFeishuStatus(status);
      if (status.state !== 'ready') setFeishuBriefing(null);
    } catch (error) {
      setFeishuError(error instanceof Error ? error.message : '无法检查飞书服务。');
    } finally {
      setFeishuBusy(false);
    }
  };

  const refreshFeishuBriefing = async (): Promise<void> => {
    setFeishuBusy(true);
    setFeishuError('');
    try {
      const briefing = await window.xiaofei.feishu.getBriefing();
      setFeishuStatus(briefing.status);
      setFeishuBriefing(briefing);
      pushEvent({
        title: '飞书工作简报已刷新',
        detail: `${briefing.meetings.length} 项今日日程，${briefing.tasks.length} 项未完成任务${briefing.warnings.length ? `；${briefing.warnings.join('；')}` : ''}`,
        tone: briefing.warnings.length ? 'amber' : 'violet',
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : '飞书简报刷新失败。';
      setFeishuError(message);
      pushEvent({ title: '飞书简报刷新失败', detail: message, tone: 'coral' });
    } finally {
      setFeishuBusy(false);
    }
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

  // 设置面板每次读到/写完配置都回传一次：链路节点照着真实的生效地址走，
  // 不再靠一个只存在于渲染进程内存里、谁都没在用的 savedUrl。
  const applyServerResolution = (resolution: AppConfigResolution): void => {
    const next = resolution.serverUrl;
    setServerUrl((current) => (
      current !== null && current.value === next.value && current.source === next.source
        ? current
        : next
    ));
  };

  return (
    <div className="desktop-shell">
      <AppSidebar
        activePage={activePage}
        agentHookCount={activeAgentHookCount}
        cameraMonitoring={{
          enabled: cameraMonitoring.enabled,
          connection: cameraMonitoring.connection,
        }}
        onNavigate={setActivePage}
      />
      <div className="desktop-stage">
      {activePage === 'camera' ? (
        <CameraPage />
      ) : activePage === 'focus' ? (
        <div className="app-shell focus-shell">
          <header className="topbar">
            <div className="view-title">
              <h1>番茄钟</h1>
              <span>专注与休息节奏</span>
            </div>
            <div className="topbar-status">
              <span className="runtime-label">Electron {runtime.versions.electron}</span>
            </div>
          </header>
          <main className="focus-page">
            <PomodoroPanel />
          </main>
        </div>
      ) : activePage === 'incident' ? (
        <div className="app-shell focus-shell">
          <header className="topbar">
            <div className="view-title">
              <h1>告警管理</h1>
              <span>两条告警链路的合并视图与只读诊断</span>
            </div>
            <div className="topbar-status">
              <span className="runtime-label">Electron {runtime.versions.electron}</span>
            </div>
          </header>
          <main className="focus-page">
            <IncidentPanel />
          </main>
        </div>
      ) : activePage === 'away' ? (
        <div className="app-shell focus-shell">
          <header className="topbar">
            <div className="view-title">
              <h1>返岗汇总</h1>
              <span>离席期间发生的事，按播报优先级排好</span>
            </div>
            <div className="topbar-status">
              <span className="runtime-label">Electron {runtime.versions.electron}</span>
            </div>
          </header>
          <main className="focus-page">
            <AwaySummaryPanel />
          </main>
        </div>
      ) : (
      <div className="app-shell">
      <header className="topbar">
        <div className="view-title">
          <h1>今天</h1>
          <span>你的工作状态与桌面伙伴</span>
        </div>
        <div className="topbar-status">
          {cameraMonitoring.enabled && (
            <button
              className={`status-pill camera-monitoring-pill ${cameraMonitoring.connection === 'online' ? 'is-live' : 'is-idle'}`}
              type="button"
              onClick={() => setActivePage('camera')}
            >
              <Camera size={14} aria-hidden="true" />
              {cameraMonitoring.connection === 'online' ? '摄像头监测中' : '摄像头重连中'}
            </button>
          )}
          <span className="runtime-label">Electron {runtime.versions.electron}</span>
          <span className={`status-pill ${activeAgentHookCount > 0 ? 'is-live' : 'is-idle'}`}>
            <i />{activeAgentHookCount > 0 ? 'Agent 监控中' : '系统待连接'}
          </span>
        </div>
      </header>

      <main>
        <section className="hero-grid">
          <div className="hero-copy">
            <p className="eyebrow"><Bot size={15} aria-hidden="true" /> 桌面伙伴</p>
            <h2>工伴已在工位上</h2>
            <p className="hero-summary">
              正在留意 Agent 任务和你的在岗状态。需要操作时会在这里明确提醒，不会擅自代你确认。
            </p>

            <div className="neural-link" aria-label="系统连接链路">
              <div className="link-node">
                <span className="link-node-icon"><span>1</span></span>
                <strong>Electron</strong>
                <small>本机在线</small>
              </div>
              <div className="link-line"><i /></div>
              <div className="link-node">
                <span className="link-node-icon"><span>2</span></span>
                <strong>Server</strong>
                <small>{serverUrl ? serverUrl.value : '读取配置中…'}</small>
              </div>
              <div className="link-line is-muted"><i /></div>
              <div className="link-node is-muted">
                <span className="link-node-icon"><span>3</span></span>
                <strong>Deskbot</strong>
                <small>WebSocket 离线</small>
              </div>
            </div>
          </div>

          <ServerSettingsPanel onResolved={applyServerResolution} />
        </section>

        <section className="agent-monitor" id="agent-monitor">
          <div className="section-heading agent-heading">
            <div>
              <span className="section-index">LOCAL AGENT HOOKS</span>
              <h2>AI Agent 任务监控</h2>
            </div>
            <div className="agent-heading-actions">
              <span>{activeAgentHookCount} / 3 已生效</span>
              <button
                className="primary-action"
                type="button"
                disabled={agentBusy !== null}
                onClick={() => void installAllAgentHooks()}
              >
                <PlugZap size={15} aria-hidden="true" />
                {agentBusy === 'all' ? '正在配置…' : '自动发现并接入'}
              </button>
            </div>
          </div>

          {agentError && <p className="agent-error" role="alert">{agentError}</p>}

          <div className="agent-source-grid">
            {(['codex', 'claude-code', 'workbuddy'] as const).map((source) => {
              const detection = agentSnapshot.installations.find((item) => item.source === source);
              const installed = detection?.installed ?? false;
              const awaitingTrust = detection?.requiresTrustReview ?? false;
              return (
                <article className={`agent-source-card ${installed ? 'is-installed' : ''} ${awaitingTrust ? 'is-pending' : ''}`} key={source}>
                  <div>
                    <span className="agent-source-code">{source.toUpperCase()}</span>
                    <span className={`agent-install-state ${installed ? 'is-installed' : ''} ${awaitingTrust ? 'is-pending' : ''}`}>
                      <i />{awaitingTrust ? '等待授权' : installed ? '正在监控' : detection?.available ? '可接入' : '未发现'}
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
                    <PlugZap size={14} aria-hidden="true" />
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

        <section className="feishu-workspace" id="feishu-workspace">
          <div className="section-heading feishu-heading">
            <div>
              <span className="section-index">FEISHU OPENAPI / SERVER</span>
              <h2>飞书任务与会议</h2>
            </div>
            <div className="feishu-actions">
              <span className={`feishu-state state-${feishuStatus?.state ?? 'checking'}`}>
                <i />{feishuStatus?.state === 'ready' ? 'Server 已连接' : feishuStatus ? '需要配置' : '正在检查'}
              </span>
              <button type="button" disabled={feishuBusy} onClick={() => void checkFeishuConnection()}>
                <PlugZap size={14} aria-hidden="true" />检查连接
              </button>
              <button className="primary-action" type="button" disabled={feishuBusy || feishuStatus?.state !== 'ready'} onClick={() => void refreshFeishuBriefing()}>
                <RefreshCw size={14} aria-hidden="true" />{feishuBusy ? '读取中…' : '刷新数据'}
              </button>
            </div>
          </div>

          {feishuStatus && (
            <div className="feishu-identity">
              <div>
                <span>当前用户</span>
                <strong>{feishuStatus.openId ?? '尚未配置'}</strong>
              </div>
              <div>
                <span>数据来源</span>
                <strong>Server OpenAPI</strong>
              </div>
              <p>{feishuStatus.message}</p>
            </div>
          )}

          {feishuError && <p className="feishu-alert is-error" role="alert">{feishuError}</p>}
          {feishuStatus?.missingScopes.length ? (
            <div className="feishu-alert">
              <strong>需要补充只读权限</strong>
              <p>{feishuStatus.missingScopes.join('、')}</p>
              <code>请在飞书开发者后台开通权限，并更新 Server 用户授权</code>
            </div>
          ) : null}
          {feishuBriefing?.warnings.map((warning) => (
            <p className="feishu-alert" key={warning}>{warning}</p>
          ))}

          <div className="feishu-data-grid">
            <article className="feishu-data-card">
              <div className="task-card-heading">
                <span>TODAY'S MEETINGS</span>
                <small>{feishuBriefing?.meetings.length ?? 0} 项</small>
              </div>
              {feishuBriefing?.meetings.length ? (
                <ol>
                  {feishuBriefing.meetings.slice(0, 10).map((meeting) => (
                    <li key={meeting.id}>
                      <time>{formatFeishuTime(meeting.start, meeting.allDay)}</time>
                      <div>
                        {meeting.url ? <a href={meeting.url} target="_blank" rel="noreferrer">{meeting.title}</a> : <strong>{meeting.title}</strong>}
                        {meeting.end && !meeting.allDay && <small>至 {formatFeishuTime(meeting.end)}</small>}
                      </div>
                    </li>
                  ))}
                </ol>
              ) : (
                <p className="feishu-empty">{feishuBriefing ? '今天没有日程' : '连接后读取今日日程'}</p>
              )}
            </article>

            <article className="feishu-data-card">
              <div className="task-card-heading">
                <span>OPEN TASKS</span>
                <small>{feishuBriefing?.tasks.length ?? 0} 项</small>
              </div>
              {feishuBriefing?.tasks.length ? (
                <ol>
                  {feishuBriefing.tasks.slice(0, 12).map((task) => (
                    <li key={task.id}>
                      <span className="feishu-task-mark" aria-hidden="true" />
                      <div>
                        {task.url ? <a href={task.url} target="_blank" rel="noreferrer">{task.title}</a> : <strong>{task.title}</strong>}
                        <small>{task.projectName ? `${task.projectName} · ` : ''}{formatFeishuDue(task.due, task.allDay)}</small>
                      </div>
                    </li>
                  ))}
                </ol>
              ) : (
                <p className="feishu-empty">{feishuBriefing ? '当前没有未完成任务' : '连接后读取待办任务'}</p>
              )}
            </article>
          </div>
        </section>

        <section className="workspace-grid">
          <div className="capabilities">
            <div className="section-heading">
              <div>
                <span className="section-index">{visibleFeatureCatalog.length} CAPABILITIES</span>
                <h2>已接入能力</h2>
              </div>
              <p>每张卡片都连着真实链路，点了就是真发生</p>
            </div>

            <div className="feature-grid">
              {visibleFeatureCatalog.map((feature) => (
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
                      if (feature.id === 'focus-mode') setActivePage('focus');
                      if (feature.id === 'incident-assistant') setActivePage('incident');
                      if (feature.id === 'away-summary') setActivePage('away');
                      if (feature.id === 'feishu-briefing') {
                        focusFeishuWorkspace();
                        void refreshFeishuBriefing();
                      }
                      void triggerFeature(feature);
                    }}
                    aria-pressed={activeFeature.id === feature.id}
                  >
                    {feature.triggerLabel}<ArrowUpRight size={14} aria-hidden="true" />
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
              <p>{activeFeature.summary}</p>
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
        <span>Agent Hook 已本机接入 / Server 地址与设备号统一取自配置中心</span>
      </footer>
      </div>
      )}
      </div>
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
