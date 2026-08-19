# 告警值班中继 实施计划

设计见 [2026-08-18-alert-oncall-relay-design.md](../specs/2026-08-18-alert-oncall-relay-design.md)。
按 CLAUDE.md 的约定：**先让测试红，再实现**，Python 侧用 pytest。

## 任务

- [x] 1. `core/alert_relay/models.py`：`AlertEvent` / `RelayState` / `RelayRecord` / `Diagnosis`
      测试：`tests/test_alert_relay_models.py`（指纹派生、状态流转合法性、诊断 JSON 归一）
- [x] 2. `core/alert_relay/parser.py`：SAE 告警原文 → `AlertEvent`
      测试：`tests/test_alert_relay_parser.py`（pod→workload、集群→projectId/clusterId、缺字段容错）
- [x] 3. `core/alert_relay/cards.py`：告警卡片 / 结论卡片 / 失败卡片模板
      测试：`tests/test_alert_relay_cards.py`（按钮 value 带 alert_id、severity 配色、空数组不渲染空块）
- [x] 4. `core/alert_relay/feishu_bot.py`：tenant_access_token 缓存、发卡片、回帖、加表情回执
      测试：`tests/test_alert_relay_feishu_bot.py`（aiohttp 假服务端，令牌复用与过期重取、错误码转异常）
- [x] 5. `core/alert_relay/robot.py`：硬件桥，severity → 表情/动作，设备离线不抛
      测试：`tests/test_alert_relay_robot.py`（假 registry/conn，离线返回 False 不抛）
- [x] 6. `core/alert_relay/diagnosis_runner.py`：调起本机 Claude Code，解析 `--output-format json`
      测试：`tests/test_alert_relay_runner.py`（假 CLI 脚本，成功/超时/非零退出/输出非 JSON）
- [x] 7. `core/alert_relay/service.py`：状态机编排 + 去重 + 超时升级 + 回复意图识别
      测试：`tests/test_alert_relay_service.py`（全链路、去重、拒绝、超时、诊断失败）
- [x] 8. `core/api/alert_relay_handler.py` + `core/alert_relay_routes.py`：四个 HTTP 端点
      测试：`tests/test_alert_relay_handler.py`（鉴权、url_verification 挑战、卡片回调、状态查询）
- [x] 9. `core/alert_relay/factory.py` + `config.yaml` 配置块 + `core/http_server.py` 接线
      测试：`tests/test_alert_relay_config.py`（环境变量优先、默认值、未配置时 health 降级）
- [x] 10. 文档：`docs/api/告警值班中继接口.md`、README 回写、AGENTS.md 补接口表
- [x] 11. `run_alert_relay_check.py`：全链路模拟脚本（假飞书 + 假机器人 + 真/假 CLI）

## 验证

```bash
cd server/main/xiaozhi-server && python -m pytest tests/test_alert_relay_*.py -q
cd server/main/xiaozhi-server && python -m pytest tests/ -q          # 不回归既有 morning_brief / presence

python run_alert_relay_check.py              # 秒级全链路模拟（假 CLI）
python run_alert_relay_check.py --real-cli   # 用真的 Claude Code 跑一遍
```

联调（真机 + 真飞书）：

```bash
curl -X POST http://127.0.0.1:8003/xiaozhi/alert/ingest \
  -H "Content-Type: application/json" -d '{"raw_text":"告警集群：bj-jxq-autocar\n..."}'
```

## 真机/真 CLI 验证记录（2026-08-18）

跑 `--real-cli` 抓到三个只有真跑才会暴露的问题，都已修：

1. **Windows 上 `claude` 起不来。** npm 装的是 `claude.CMD`，而
   `create_subprocess_exec` 不走 shell、不做 PATHEXT 补全，直接 `WinError 2`。
   → 启动前用 `shutil.which()` 解析可执行文件。
2. **工具白名单漏了 PowerShell。** skill 是用 **PowerShell 工具**跑 `sae.ps1` 拉日志的，
   而 `--permission-mode dontAsk` 会拒掉白名单外的工具。漏掉的后果不是报错，而是
   agent 一条日志都拉不到、只能回一张「什么都查不了」的失败卡片——链路看着通，诊断永远是空的。
   → 默认白名单补 `PowerShell`。
3. **告警原文是不可信输入。** 真 CLI 把一条「像指令的告警」识别成提示词注入并拒绝诊断。
   任何人只要能让一行文本进线上日志，就能把内容送进这段提示词。
   → 提示词里显式声明原文是「被诊断的对象」而非指令。

同时验证了设计里最重要的那条：**诊断跑不成时回的是失败卡片，不是编出来的根因**——
第一次真跑因为拿不到日志和源码，卡片如实写了「什么被挡住了」，机器人摇头播报「没查成」。

补完白名单后第二次真跑**全链路打通**：agent 真的用 PowerShell 跑 `sae.ps1` 拉到了
现网日志，并且认出送进去的是一条构造出来的样例告警——

> 根因：告警描述的事件在现网不存在——命名空间、pod、时间点、条数四项均与日志矛盾，
> 疑为样例/回放告警而非真实故障。全天唯一一笔真超时是 18:15:47 的 edittext 任务，
> 因下游 swaptext engine 受理后 203 秒未回调被超时清扫转失败，与限流、DB 均无关。

结论卡片按契约渲染完整（时间轴、为什么、已排除、建议），机器人走完
`shocked → happy → thinking → confident`，最后播报「查清了：……」。
源码没挂载时 `why.code` 会写成「未挂载源码」，这也说明 `source_dirs` 必须配对。

## 换机器验证（2026-08-19，来自真实反馈）

把分支拿到另一台 Mac 上，**单测和假 CLI 链路都能跑，真实告警诊断却空转到超时失败**。
原因是诊断依赖两样只存在于作者 Windows 机器上的东西：个人目录里的
`~/.claude/skills/diagnose-sae-alert`，和它调用的 Windows 专用 `sae.ps1`。
仓库里一份都没有，所以别人克隆下来必然跑不了。

三处修改：

1. **诊断能力随仓库分发。** 新增 `.claude/skills/diagnose-sae-alert/`，其中
   `scripts/sae_logs.py` 是 `sae.ps1` + `fetch_prod_logs.py` 的跨平台替代——纯标准库、
   只发 GET、三个平台通用；凭证仍只从环境变量或 `~/.sae` 读，不入库。
2. **缺依赖秒级失败。** `ClaudeCodeRunner.preflight()` 检查 CLI / skill / SAE 凭证
   （硬性）和 source_dirs（降级警告），任一硬性项缺失就不起子进程，直接回失败卡片
   列出缺什么。超时失败最难判——看着像模型慢，实际是依赖压根不存在。
3. **工作目录默认改成仓库根**，否则 Claude Code 找不到项目级 skill。

验证方式：把个人的两个 skill 临时改名藏起来（模拟别人的电脑），只留仓库自带的那份，
用真 Claude Code 跑 `run_alert_relay_check.py --real-cli` —— **全链路打通**，
诊断基于真实日志给出时间轴（18:15:47 提交 → engine 空回调 → 18:19:10 超时 200 秒判失败）。
跑完自动还原个人 skill。

## 风险

- **只有一块开发板**：硬件那段先用假 conn 单测覆盖，真机验证攒到最后一次性做。
- **`http_server.py` 是共享文件**：改之前 `git fetch`，只增不覆盖（CLAUDE.md 明写的坑）。
- **Claude Code 子进程**：不同机器 CLI 路径不同，路径与参数全部可配，默认值只是默认值。
- **诊断质量取决于 `source_dirs`**：没挂被诊断服务的源码，skill 只能靠日志硬猜，
  多半会如实回「查不了」。这不是 bug，但配置时必须填对。
