# 操作链路顺滑化实施计划（ops-smoothness）

对应设计：`../specs/2026-08-28-ops-smoothness-design.md`。分四路并行 + 桌面端二阶段，全部测试先行（vitest / pytest），提交按 Conventional Commits 分域。

- [ ] A1 desktop 配置中心：配置存储（原子 JSON + env>file>默认 三级取值）→ 五条链路改造 → 设置面板做实（IPC 读写、来源展示、摄像头热切换）→ 存量 env 相关测试更新
- [ ] A2 desktop 交互补全（依赖 A1）：外部探针 needs_user 通知机器人；返岗汇总面板（client+IPC+Panel）；mock 模块隐藏
- [ ] B server 状态落盘：基态 / 番茄钟会话 / alert_relay 记录三件，重启恢复语义各自建回归测试；alert_relay 纳入 on_cleanup
- [ ] C 启动器与演示：仓库根 `gongban` 六个子命令；`tools/mock_device.py` 模拟设备；两份拍摄脚本移植进 `tools/` 并去除绝对路径
- [ ] D dev 摄像头根因定位（只读排查，出证据与修复方案；主观画面验收进人工清单）
- [ ] 质量门：desktop `npm test && npm run typecheck && npm run package`；server `python -m pytest tests/`
- [ ] 对抗审查一轮，修复确认缺陷
- [ ] 文档同步：AGENTS.md（环境变量契约→配置中心、desktop 职责、启动器）、CLAUDE.md 常用命令

注意：plan 复选框状态照例不可信，以代码为准。
