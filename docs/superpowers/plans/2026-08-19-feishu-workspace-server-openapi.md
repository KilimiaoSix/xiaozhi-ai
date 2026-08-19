# 飞书任务与会议服务端 OpenAPI 迁移实施计划

设计见 [2026-08-19-feishu-workspace-server-openapi-design.md](../specs/2026-08-19-feishu-workspace-server-openapi-design.md)。

## 任务

- [x] 1. 为 `FeishuClient` 增加任务分页和任务清单详情测试与实现。
- [x] 2. 新增 `FeishuWorkspaceService`，覆盖正常聚合、单源失败、清单名降级和双源失败。
- [x] 3. 新增 status/briefing Handler 与路由，覆盖认证、日期校验和错误信封。
- [x] 4. 接入 `SimpleHttpServer`，复用晨报用户令牌配置。
- [x] 5. 新增 Desktop `FeishuHttpClient`，覆盖响应转换、Bearer 和离线错误。
- [x] 6. 删除 `LarkCliClient`，主进程和 IPC 改用 Server HTTP Client。
- [x] 7. 更新 UI 文案、README、功能清单、API 文档和 AGENTS.md。
- [x] 8. 运行 Server/desktop 测试、类型检查、打包和静态 `lark-cli` 引用检查。

## 验证

```bash
cd server/main/xiaozhi-server
python -m pytest tests/test_feishu_workspace_*.py tests/test_morning_brief_feishu_client.py tests/test_http_server_assembly.py -q

cd desktop
npm test -- --run src/modules/features/feishu-briefing src/main/feishuIpc.test.ts
npm run typecheck
npm run package

rg -n "lark-cli|LarkCli" desktop/src desktop/forge.config.ts desktop/package.json
```

## 验证记录（2026-08-19）

- Server 全量：`860 passed`。
- Desktop 飞书定向：5 个文件、8 项测试通过；新增 HTTP Client 自身 3 项通过。
- Desktop 全量：`197 passed / 1 failed`；唯一失败是并行在制的
  `CameraPage.test.tsx`“测试久坐提醒”用例，与本迁移无关。
- `npm run package`：macOS arm64 打包成功。
- `npm run typecheck`：被并行在制的 `wellbeingTestService` 缺文件和 `CameraPage`
  新增 props 未接线阻塞；飞书定向测试、生产 Vite 构建和打包均通过。
- Desktop 源码静态检查：无 `lark-cli`、`LarkCli`、`FeishuCliStatus` 引用。
