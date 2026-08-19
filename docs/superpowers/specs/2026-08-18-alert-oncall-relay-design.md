# 告警值班中继（Alert On-call Relay）设计

日期：2026-08-18
状态：设计
Scope：`server`

## 一句话

SAE 告警进来后，**机器人抬头提醒人 + 飞书发卡片给人**；人在飞书上回一句"帮我查"，
服务端就**调起本机的 Claude Code**（复用已有的 `diagnose-sae-alert` skill）做只读根因诊断，
再把诊断结论回帖成飞书卡片，同时让机器人点头播报结论。

## 为什么要有它

现状是三段各自独立、中间靠人手工搬运：

1. SAE 告警只落在飞书告警群里，**人不在工位就看不见**——本项目的机器人恰好是个物理提醒器官，但没接告警。
2. `diagnose-sae-alert` skill 已经能查清根因，但它要人**手工把告警文本贴进 Claude Code** 才会跑。
3. 诊断结论产出后又要人**手工贴回飞书**。

这条中继把三段接起来，并且**保留人的决策点**：机器人和卡片只负责"叫人"，
真正开跑诊断必须由人回一句话——符合 AGENTS.md「涉及授权或高风险操作时，只提醒用户，不代替用户确认」。

## 边界（不做什么）

- **不自动修复**。沿用 skill 的只读红线：只 grep 代码、只读日志、只发 GET。产出仅为文字诊断。
- **不做告警规则配置**。告警的产生仍在 SAE 侧，本模块只是接收端。
- **不在服务端做 LLM 诊断**。诊断能力来自本机 Claude Code 子进程，服务端只负责编排与传话。
- **不替代飞书告警群**。卡片是额外的定向通知，不接管原告警链路。

## 全链路

```
SAE 告警
  │ POST /xiaozhi/alert/ingest
  ▼
AlertRelayService ──► 指纹去重（同集群+同 workload+同关键词，窗口内合并）
  │
  ├──► 机器人（硬件）   push_work_event(emotion=shocked, action=look_up, speak=True)
  │                    "线上告警：iflyplot-ai 无痕改字处理超时"
  │
  └──► 飞书            interactive 卡片 → 值班人（含「帮我查」「我自己看」按钮）
  │
  ▼  等人回复（卡片按钮 / 群内文本回复 / @机器人）
  │  POST /xiaozhi/alert/feishu/callback
  │
  ├─ 回「我自己看」 ─► DECLINED，机器人点头收工
  ├─ 超时未回复    ─► 机器人再提醒一次 + 卡片催办（默认不自动开跑）
  └─ 回「帮我查」  ─► CLAIMED
        │  机器人：happy + nod +「收到，我去查」；飞书：回一个 OK 表情作回执
        ▼
     DIAGNOSING —— 调起本机 Claude Code（headless）
        │  claude -p "<告警原文>" --output-format json --permission-mode dontAsk
        │  提示词里点名使用 diagnose-sae-alert skill；--add-dir 挂 iflyplot-server 源码
        │  机器人：thinking，状态栏「排查中」
        ▼
     诊断 JSON（skill 的既有输出契约：title/severity/why/root_cause/suggestion...）
        │
        ├──► 飞书：渲染成彩色结论卡片，回在告警卡片的同一话题下
        └──► 机器人：confident + nod + 播报 root_cause 的一句话
```

## 关键设计决策

### 1. 诊断由本机 Claude Code 子进程执行，而不是服务端自己调 LLM

服务端没有代码库上下文、没有 SAE 凭证、也没有 skill 里那套踩坑知识。
`diagnose-sae-alert` skill 已经把环境、命令、映射表、输出契约全写死了，重写一份必然走样。
所以本模块把诊断**外包给本机 Claude Code**，自己只做三件事：拼提示词、管超时、解析它吐出的 JSON。

代价：服务端必须与 Claude Code 跑在同一台机器（值班人的开发机），且该机器能连内网 SAE。
这正是当前的真实部署形态——服务端本来就跑在开发机上。

### 2. 人必须回复才开跑，超时不自动开跑

告警风暴时自动开跑会同时拉起 N 个 Claude Code 进程，既烧钱又会把 SAE 只读接口打满。
默认 `auto_diagnose_on_timeout: false`，超时只升级提醒。

### 3. 指纹去重，避免硬件被告警风暴刷屏

指纹 = `集群 | 命名空间 | workload | 关键词`。窗口（默认 300 秒）内重复告警**只累加计数**，
不再触发机器人动作、也不再发新卡片。机器人是物理设备，被刷屏的代价是用户直接把它关掉。

pod 名和告警时间刻意不参与指纹：它们每次都变，算进去就永远去不了重。
这也意味着 **pod → workload 的推导失败会连带毁掉去重**——所以那条规则要容忍随机后缀长度。

### 4. 告警对象 → workload 的推导沿用 skill 的规则

pod 名 `iflyplot-ai-7d9f8b6c5d-x2k9p` 去掉 `-<rs hash>-<suffix>` 得 workload `iflyplot-ai`。
这条规则 skill 里已经验证过，两边必须一致，否则拉日志的 label 过滤会落空。

### 5. 飞书用 bot 身份（tenant_access_token），与晨报的 user token 分离

晨报读的是"我的消息"，必须用户令牌；告警中继是"机器人主动发消息"，用应用令牌。
两者权限体系不同，客户端也不复用——`morning_brief/feishu_client.py` 保持只读不动。

## 状态机

| 状态 | 含义 | 出边 |
|---|---|---|
| `RECEIVED` | 已接收并解析 | → `NOTIFIED` |
| `NOTIFIED` | 卡片已发、机器人已提醒 | → `AWAITING_REPLY` |
| `AWAITING_REPLY` | 等人回复 | → `CLAIMED` / `DECLINED` / `TIMEOUT` |
| `CLAIMED` | 人已认领并要求排查 | → `DIAGNOSING` |
| `DIAGNOSING` | Claude Code 子进程运行中 | → `DIAGNOSED` / `FAILED` |
| `DIAGNOSED` | 结论已回帖 | 终态 |
| `DECLINED` | 人表示自己看 | 终态 |
| `TIMEOUT` | 超时无人认领（已升级提醒） | → `CLAIMED`（人后来回了） |
| `FAILED` | 诊断进程失败/超时/输出不合契约 | 终态（卡片写明失败原因） |

## 硬件语汇（在 AGENTS.md 的 21 表情 / 既有动作里选，不新增）

| 时机 | status | emotion | action | speak |
|---|---|---|---|---|
| 告警到达（紧急/严重） | 线上告警 | `shocked` | `look_up` | 是 |
| 告警到达（警告） | 线上告警 | `confused` | `look_up` | 否 |
| 人已认领 | 排查中 | `happy` | `nod` | 是 |
| 诊断进行中 | 排查中 | `thinking` | — | 否 |
| 诊断完成 | 排查完成 | `confident` | `nod` | 是 |
| 诊断失败 | 排查失败 | `sad` | `shake` | 是 |
| 人自己看 | 待机 | `neutral` | `nod` | 否 |

超时升级只重发一次告警到达那组，并把 status 改成「还没人接」。

## 接口

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/xiaozhi/alert/ingest` | 告警接入（SAE webhook 或手工 curl） |
| POST | `/xiaozhi/alert/feishu/callback` | 飞书事件订阅 + 卡片按钮回调 |
| GET | `/xiaozhi/alert/{alert_id}` | 查单条中继状态（联调用） |
| GET | `/xiaozhi/alert/health` | 依赖就绪度自检 |

`ingest` 请求体（`raw_text` 必填，其余可选，给不出就由 `raw_text` 解析）：

```jsonc
{
  "raw_text": "【告警】告警等级：严重\n告警集群：bj-jxq-autocar\n...",
  "level": "严重",
  "cluster": "bj-jxq-autocar",
  "namespace": "iflyplot",
  "target": "iflyplot-ai-7d9f8b6c5d-x2k9p",
  "keyword": "无痕改字处理超时",
  "alert_time": "2026-08-18 21:00:00",
  "policy_url": "https://one.iflytek.com/...?projectId=117&clusterId=3"
}
```

## 失败模式与兜底

| 失败 | 表现 | 兜底 |
|---|---|---|
| 机器人不在线 | `device_registry.get()` 返回 None | 飞书照发，卡片上标注「机器人离线」 |
| 飞书发卡片失败 | OpenAPI 非 0 code | 机器人照提醒，状态置 `NOTIFIED` 但记 `notify_error` |
| Claude Code 不存在/超时 | 子进程非 0 或超时 | `FAILED`，卡片回帖写明失败原因与手工重跑命令 |
| 诊断输出不是合法 JSON | 解析失败 | `FAILED`，把前 500 字原文附在卡片里，不猜 |
| 两路通知全挂 | — | `ingest` 仍返回 200 并在响应里带 `warnings`，告警不能因为通知失败而丢 |

## 安全

- 复用 `server.auth` 的 Bearer 校验保护 `ingest`；飞书回调另用 `verification_token` 校验。
- bot 的 `app_secret`、SAE 凭证一律走环境变量 / `data/.config.yaml`，不进 `config.yaml`。
- 子进程以 `--permission-mode dontAsk` + 工具白名单启动，禁止写操作；诊断红线由 skill 自身守。
- 卡片内容不含数据库连接串、原始 SQL、令牌（skill 输出契约已有同款约束）。
