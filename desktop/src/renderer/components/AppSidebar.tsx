import {
  Bot,
  Camera,
  House,
  Inbox,
  Siren,
  Timer,
  Wifi,
  WifiOff,
} from 'lucide-react';

export type AppPage = 'dashboard' | 'focus' | 'incident' | 'away' | 'camera';

interface AppSidebarProps {
  activePage: AppPage;
  agentHookCount: number;
  cameraMonitoring: {
    enabled: boolean;
    connection: 'idle' | 'connecting' | 'online' | 'reconnecting';
  };
  onNavigate: (page: AppPage) => void;
}

const navigation = [
  { id: 'dashboard', label: '今天', icon: House },
  { id: 'focus', label: '番茄钟', icon: Timer },
  { id: 'incident', label: '告警', icon: Siren },
  { id: 'away', label: '返岗', icon: Inbox },
  { id: 'camera', label: '摄像头', icon: Camera },
] as const;

export function AppSidebar({
  activePage,
  agentHookCount,
  cameraMonitoring,
  onNavigate,
}: AppSidebarProps) {
  const cameraOnline = cameraMonitoring.enabled
    && cameraMonitoring.connection === 'online';
  const cameraLabel = cameraMonitoring.enabled
    ? cameraOnline ? '摄像头监测中' : '摄像头重连中'
    : '摄像头未启用';

  return (
    <aside className="app-sidebar">
      <div className="sidebar-drag-region" aria-hidden="true" />
      <div className="sidebar-brand" aria-label="工伴桌面精灵">
        <span className="sidebar-brand-icon" aria-hidden="true"><Bot size={18} /></span>
        <div>
          <strong>工伴</strong>
          <span>桌面精灵</span>
        </div>
      </div>

      <nav className="sidebar-navigation" aria-label="主导航">
        <span className="sidebar-section-label">工作区</span>
        {navigation.map(({ id, label, icon: Icon }) => (
          <button
            className={activePage === id ? 'is-active' : ''}
            type="button"
            aria-label={label}
            aria-current={activePage === id ? 'page' : undefined}
            onClick={() => onNavigate(id)}
            key={id}
          >
            <Icon size={17} strokeWidth={1.8} aria-hidden="true" />
            <span>{label}</span>
          </button>
        ))}
      </nav>

      <div className="sidebar-status" aria-label="系统状态">
        <span className="sidebar-section-label">系统状态</span>
        <div>
          <span className={`sidebar-status-icon ${agentHookCount > 0 ? 'is-online' : ''}`}>
            {agentHookCount > 0 ? <Wifi size={14} /> : <WifiOff size={14} />}
          </span>
          <p>
            <strong>{agentHookCount} 个 Agent 已连接</strong>
            <small>本机任务监控</small>
          </p>
        </div>
        <div>
          <span className={`sidebar-status-icon ${cameraOnline ? 'is-online' : ''}`}>
            <Camera size={14} />
          </span>
          <p>
            <strong>{cameraLabel}</strong>
            <small>画面仅在内存流转</small>
          </p>
        </div>
      </div>

      <footer className="sidebar-footer">
        <span>LaunchCrush</span>
        <small>0.1.0</small>
      </footer>
    </aside>
  );
}
