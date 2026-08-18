import { useMemo, useState } from 'react';

import { featureCatalog, type FeatureDefinition } from '../shared/features';

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

export function App() {
  const [activeFeature, setActiveFeature] = useState(featureCatalog[0]);
  const [events, setEvents] = useState(initialEvents);
  const [serverUrl, setServerUrl] = useState('http://192.168.1.100:8003');
  const [savedUrl, setSavedUrl] = useState('');

  const runtime = useMemo(() => window.xiaofei.getRuntimeInfo(), []);

  const triggerFeature = (feature: FeatureDefinition): void => {
    setActiveFeature(feature);
    setEvents((current) => [
      {
        id: Date.now(),
        time: timeLabel(),
        title: `${feature.title} · Mock 已触发`,
        detail: '当前只记录演示事件，后续在对应适配器中接入真实能力。',
        tone: feature.tone,
      },
      ...current,
    ].slice(0, 5));
  };

  const saveServerUrl = (): void => {
    const normalizedUrl = serverUrl.trim();
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
          <span className="status-pill is-idle"><i />系统待连接</span>
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
                    <span className="placeholder-state"><i />待接入</span>
                  </div>
                  <h3>{feature.title}</h3>
                  <p>{feature.summary}</p>
                  <button
                    type="button"
                    onClick={() => triggerFeature(feature)}
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
              <p>扩展入口已经预留，可从 Mock 触发逐步替换为真实适配器。</p>
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
        <span>所有外部能力当前均为安全占位</span>
      </footer>
    </div>
  );
}
