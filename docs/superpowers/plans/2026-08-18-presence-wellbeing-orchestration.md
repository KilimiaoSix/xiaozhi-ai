# 基于本人在岗识别的情绪关怀编排实现计划

日期：2026-08-18

## 任务

- [x] 扩展本人核验结果与 PresenceRegistry schema，提供 `horizontal_position`。
- [x] 新增纯规则的 wellbeing policy、状态机与事件模型。
- [x] 新增异步服务，完成工位设备绑定、轮询、下发与生命周期管理。
- [x] 在默认配置中加入克制且可覆盖的关怀参数。
- [x] 覆盖久坐、有效休息、下班、21 点、23 点强提醒、暖心互动和方向动作测试。
- [x] 更新摄像头 Presence API、README、功能清单和 AGENTS 事实源。
- [x] 运行 Server 相关测试与 desktop 回归测试。
