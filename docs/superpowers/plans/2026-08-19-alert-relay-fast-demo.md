# 告警诊断快速演示模式 Implementation Plan

> **For agentic workers:** 按仓库规则在当前会话内顺序执行；不创建子代理、worktree、提交或 PR。

**Goal:** 默认用一次生产日志查询在 60 秒内返回精简诊断，同时保留可显式启用的深度模式。

**Architecture:** `ClaudeCodeRunner` 根据 `fast_mode` 选择直接日志查询或 Claude Code 深挖。
快速模式直接执行 `sae_logs.py` 并做确定性汇总；深度模式保留完整 skill 和 JSON 解析。

**Tech Stack:** Python 3.10、asyncio、Claude Code CLI（仅深度模式）、pytest、YAML。

设计见 [2026-08-19-alert-relay-fast-demo-design.md](../specs/2026-08-19-alert-relay-fast-demo-design.md)。

---

### Task 1: Runner 快速模式契约

**Files:**
- Modify: `server/main/xiaozhi-server/tests/test_alert_relay_runner.py`
- Modify: `server/main/xiaozhi-server/core/alert_relay/diagnosis_runner.py`

- [ ] **Step 1: 写 RED 测试**

新增测试验证：runner 默认 `fast_mode=True`；快速模式直接执行唯一一次 `sae_logs.py` 查询，
使用告警前后 5 分钟、宽泛业务关键词和 workload label；`fast_mode=False` 仍使用现有
`diagnose-sae-alert` 深度提示词。

- [ ] **Step 2: 运行 RED**

```bash
cd server/main/xiaozhi-server
.venv/bin/python -m pytest tests/test_alert_relay_runner.py -q
```

Expected: 新增测试因直接查询与确定性汇总尚不存在而失败。

- [ ] **Step 3: 最小实现**

在 `ClaudeCodeRunner` 增加：

```python
fast_mode: bool = True
timeout_seconds: float | None = None
```

默认超时按模式选择 `55` 或 `900`；新增时间窗、宽泛关键词、直接查询和确定性汇总函数；
深度提示词与只读工具白名单保持原样。

- [ ] **Step 4: GREEN**

重新运行 `tests/test_alert_relay_runner.py -q`，预期全部通过。

### Task 2: 明确失败输出契约

**Files:**
- Modify: `server/main/xiaozhi-server/tests/test_alert_relay_runner.py`
- Modify: `server/main/xiaozhi-server/core/alert_relay/diagnosis_runner.py`

- [ ] **Step 1: 写 RED 测试**

构造 `sae_logs.py` 非零退出并断言 `RunnerResult.ok is False`，reason/detail 包含认证失败信息。

- [ ] **Step 2: 运行 RED**

```bash
.venv/bin/python -m pytest tests/test_alert_relay_runner.py -q
```

Expected: 当前快速路径尚未识别脚本退出码，测试失败。

- [ ] **Step 3: 最小实现并 GREEN**

在快速路径检查脚本退出码并返回失败结果；再次运行 runner 测试，预期全部通过。

### Task 3: 默认配置与演示入口

**Files:**
- Modify: `server/main/xiaozhi-server/tests/test_alert_relay_config.py`
- Modify: `server/main/xiaozhi-server/core/alert_relay/factory.py`
- Modify: `server/main/xiaozhi-server/config.yaml`
- Modify: `server/main/xiaozhi-server/run_alert_relay_check.py`
- Modify: `docs/api/告警值班中继接口.md`

- [ ] **Step 1: 写 RED 配置测试**

断言缺省配置构造出的 runner 为快速模式且超时 55 秒；显式 `fast_mode: false`、
`timeout_seconds: 900` 时恢复深度模式。

- [ ] **Step 2: 运行 RED**

```bash
.venv/bin/python -m pytest tests/test_alert_relay_config.py -q
```

Expected: factory 尚未传递 `fast_mode`，测试失败。

- [ ] **Step 3: 最小实现**

factory 默认读取 `fast_mode=True`；`config.yaml` 写入 `fast_mode: true` 和 `timeout_seconds: 55`。
联调脚本默认快速模式，新增 `--deep` 显式切回深度模式，未传 `--timeout` 时采用 runner 的模式默认值。
API 文档补充快速/深度配置说明。

- [ ] **Step 4: GREEN**

运行 config、runner 和 integration 测试，预期全部通过。

### Task 4: 聚焦与真实验证

**Files:**
- Verify only; no new files expected.

- [ ] **Step 1: 聚焦测试**

```bash
cd server/main/xiaozhi-server
.venv/bin/python -m pytest \
  tests/test_alert_relay_runner.py \
  tests/test_alert_relay_config.py \
  tests/test_alert_relay_integration.py -q
```

- [ ] **Step 2: 告警中继回归**

```bash
.venv/bin/python -m pytest tests/test_alert_relay_*.py -q
git diff --check
```

- [ ] **Step 3: 真实 60 秒验收**

```bash
.venv/bin/python run_alert_relay_check.py --real-cli --timeout 55
```

Expected: 命令总耗时小于 60 秒、退出码 0、最终状态 `DIAGNOSED`、输出“全链路打通”，
且结论明确列出日志数、原始关键词命中数与 Pod 对比。
