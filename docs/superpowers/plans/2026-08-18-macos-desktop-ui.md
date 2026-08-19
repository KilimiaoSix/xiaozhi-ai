# 工伴 macOS 桌面界面实施计划

> **执行方式：** 主 Agent 按批次内联执行；不使用子 Agent、worktree、commit 或 PR。

**目标：** 将 desktop 的今天页和摄像头页统一改造成完整、可用、可验证的 macOS 风格桌面工作台。

**架构：** `App` 继续拥有业务状态，在外层增加共享 `AppSidebar` 与内容舞台；`CameraPage` 变为纯页面内容。通过一套 renderer 设计令牌统一仪表盘和摄像头子组件，业务 IPC 与 Provider 契约保持不变。

**技术栈：** Electron 43、React 19、TypeScript、CSS、Lucide React、Vitest、jsdom。

**关联文档：** [Spec](../specs/2026-08-18-macos-desktop-ui-design.md) · [执行图](../graphs/2026-08-18-macos-desktop-ui.json)

---

### 任务 1：共享 App Shell 与导航

**文件：**
- 新建：`desktop/src/renderer/components/AppSidebar.tsx`
- 新建：`desktop/src/renderer/components/AppSidebar.test.tsx`
- 修改：`desktop/src/renderer/App.tsx`
- 修改：`desktop/package.json`
- 修改：`desktop/package-lock.json`

- [ ] 先写失败 DOM 测试：渲染 `AppSidebar`，断言存在“今天/摄像头”导航、当前页 `aria-current="page"`，点击摄像头触发 `onNavigate('camera')`。
- [ ] 运行 `npm test -- AppSidebar.test.tsx`，确认因组件不存在而失败。
- [ ] 安装 `lucide-react`，实现带 macOS 拖拽区、品牌、导航和连接摘要的侧栏。
- [ ] 在 `App` 外层统一渲染侧栏与内容舞台，删除摄像头页的重复导航职责。
- [ ] 运行 focused test，要求通过。

### 任务 2：今天页 macOS 信息架构

**文件：**
- 修改：`desktop/src/renderer/App.tsx`
- 修改：`desktop/src/renderer/styles.css`

- [ ] 将顶部区域改为紧凑页面工具栏和桌面伙伴状态岛，保留 Server 地址保存行为。
- [ ] 将 Agent 来源、当前任务、飞书简报、能力入口和事件流改为 macOS 分组列表/工具面板，保留现有状态 class 和事件处理器。
- [ ] 为命令按钮加入 Lucide 图标与准确的可访问名称，移除无功能装饰文本。
- [ ] 补全焦点、禁用、悬停、空状态和长文本样式。

### 任务 3：摄像头页与状态组件统一

**文件：**
- 修改：`desktop/src/modules/features/camera-capture/CameraPage.tsx`
- 修改：`desktop/src/modules/features/camera-capture/camera.css`
- 修改：`desktop/src/modules/features/camera-capture/components/CameraPreview.tsx`
- 修改：`desktop/src/modules/features/camera-capture/components/OwnerEnrollment.tsx`
- 修改：`desktop/src/modules/features/camera-capture/components/PresenceMonitoring.tsx`
- 测试：`desktop/src/modules/features/camera-capture/**/*.test.tsx`

- [ ] 删除 CameraPage 的独立侧栏与伪导航，页面只渲染标题、设备、分段控件、预览和控制区。
- [ ] 使用相同设计令牌重写摄像头样式，确保预览比例、指标网格和错误横幅稳定。
- [ ] 为录入、取消、重置、重试和隐私状态加入 Lucide 图标，保留按钮文字和现有回调。
- [ ] 运行摄像头相关测试并修复真实回归。

### 任务 4：窗口集成与完整验证

**文件：**
- 修改：`desktop/src/main.ts`
- 必要时修改：上述 renderer 文件

- [ ] 将 BrowserWindow 背景与 macOS 材料配置对齐新主题，不改变安全 WebPreferences。
- [ ] 运行 `npm test`，要求零失败。
- [ ] 运行 `npm run typecheck`，要求零错误。
- [ ] 运行 `npm run package`，要求退出码 0。
- [ ] 启动 Electron，在 1360×880 与最小窗口检查今天页和摄像头页；操作页面切换、分段控件和滚动，确认无空白、重叠或不可达控件。
- [ ] 对照 Spec 六条验收标准检查 `git diff`，保留未提交修改供用户审核。
