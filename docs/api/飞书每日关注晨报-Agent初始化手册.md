# 飞书每日关注晨报 Agent 初始化手册

本文档面向 Codex、Claude Code、WorkBuddy 等编码 Agent。目标是让 Agent 在**用户已属于应用所在飞书组织**的前提下，复用组织现有自建应用，把仓库中的飞书每日关注晨报配置到可真实联调状态。

本文档是操作 Runbook，不替代接口定义。接口字段、能力边界和错误码详见 [飞书每日关注晨报接口](飞书每日关注晨报接口.md)。

## 1. 完成标准

只有同时满足以下条件，Agent 才能报告“晨报初始化完成”：

1. 本机存在被 Git 忽略的 `server/main/xiaozhi-server/data/.env`，且没有向终端、聊天或日志输出任何凭证值。
2. 当前用户使用自己的 OAuth 用户令牌，`FEISHU_SELF_OPEN_ID` 与该令牌对应的用户一致。
3. `data/.config.yaml` 中 `morning_brief.enabled` 为 `true`。
4. 执行 `run_morning_brief_check.py` 后：
   - `/health` 返回 HTTP 200，`status` 为 `READY`；
   - `/preview` 和 `/latest` 返回 HTTP 200；
   - `coverage_status` 为 `COMPLETE`；
   - `messages`、`mentions`、`calendar` 均为 `COMPLETE`；
   - `reauthorization_required`、`permission_required` 均为 `false`；
   - `missing_scopes` 为空。

如果日历权限明确不提供，可将 `calendar_enabled` 设为 `false`。此时日历源应为 `DISABLED`，消息源和提及源仍必须为 `COMPLETE`。

## 2. 组织现有应用

优先复用以下应用，不要擅自新建应用：

| 项目 | 值 |
| --- | --- |
| 应用名称 | `AI 写的都队机器人` |
| App ID | `cli_aa0fb31596f95cb3` |
| 开放平台域名 | `https://open.feishu.cn` |
| 已登记回调地址 | `http://localhost:3000/callback` |

App ID 不是秘密，可以写在文档和命令中。App Secret、授权码、access token、refresh token 都是凭证，禁止写进仓库或输出到对话、终端、日志和截图说明中。

组织成员身份并不自动代表拥有开发者后台权限。Agent 在操作前需要区分：

- **应用可用范围**：决定该成员能否授权和使用应用；
- **应用协作者权限**：决定该成员能否查看应用凭证、权限和安全设置。

成员只在应用可用范围内、但不是应用协作者时，可以完成用户授权，但不能自行读取 App Secret。此时应请应用负责人通过安全渠道提供凭证，或由应用负责人执行 OAuth 换票步骤；不得绕过后台权限。

## 3. 必需权限

当前晨报需要以下用户身份权限：

```text
search:message
im:message:readonly
im:message.p2p_msg:get_as_user
im:message.group_msg:get_as_user
calendar:calendar:readonly
```

其中前四项为消息和 `@我` 采集必需权限。日历不用时可以不申请最后一项，并关闭 `calendar_enabled`。

如果需要未来实现自动续期，还需申请：

```text
offline_access
```

但当前仓库只读取静态 `FEISHU_USER_ACCESS_TOKEN`，**尚未实现 refresh token 自动轮换**。Agent 不得仅开通 `offline_access` 就宣称已经支持无人值守续期；自动续期属于额外代码变更。

## 4. Agent 安全规则

执行初始化时必须遵守：

1. 先阅读仓库根目录 `AGENTS.md`，保留工作区已有修改，不提交、不建分支、不覆盖无关配置。
2. 不使用 `cat data/.env`、`env`、`printenv` 或其他会显示凭证值的命令。
3. 检查 env 时只允许输出变量名，例如：

   ```bash
   cut -d= -f1 server/main/xiaozhi-server/data/.env
   ```

4. 不把凭证放在 shell 命令参数、shell 历史、Git diff、测试 fixture 或异常消息中。
5. 写入 `.env` 后设置文件权限 `0600`，并用 `git check-ignore` 确认它被忽略。
6. 每位成员必须签发自己的用户令牌和 `open_id`；禁止复制其他成员的 `.env`。
7. 如果已有 `.env`，不得直接覆盖。先确认它是否属于当前用户；需要换用户时先取得用户明确授权。
8. 使用 Computer Use 时：
   - 读取页面和核对配置可直接进行；
   - 新增权限、发布版本、修改安全设置和确认 OAuth 授权属于持续权限变更，必须在最终动作前取得用户确认；
   - 登录凭据、验证码、扫码和 CAPTCHA 必须交给用户操作；
   - 每次点击前重新读取当前 UI 状态，不复用过期元素索引。

## 5. 初始化流程

### 5.1 确认仓库与运行环境

从仓库根目录开始：

```bash
cd server/main/xiaozhi-server
```

如果 `.venv` 不存在，根据本机操作系统创建虚拟环境并安装依赖。macOS/Linux 示例：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

不要覆盖用户已有虚拟环境。若依赖安装失败，先报告具体错误，不要跳过依赖验证后继续宣称初始化完成。

### 5.2 检查本地状态

只检查文件是否存在和变量名，不读取凭证值：

```bash
test -f data/.env && cut -d= -f1 data/.env
test -f data/.config.yaml
git check-ignore -v data/.env data/.config.yaml
```

处理规则：

- 若已有配置且真实检查全部通过，停止变更，不重复授权。
- 若 token 无效或过期，重新 OAuth 并安全替换 token。
- 若 `.env` 属于其他用户，先向用户确认是否切换身份。
- 若 `.config.yaml` 不存在，可以创建只含 `morning_brief` 的最小覆盖配置；默认值由 `config.yaml` 递归合并。

### 5.3 检查飞书应用状态

通过飞书开发者后台或可用的官方工具核对：

1. 应用已启用且当前版本已发布；
2. 当前用户位于应用可用范围；
3. 第 3 节所列权限均已开通；
4. 权限变更已经发布并完成管理员审批；
5. 安全设置中存在精确回调地址 `http://localhost:3000/callback`。

如果配置已经正确，不要做无意义的保存、重复发版或权限扩张。

若缺少权限：在取得用户确认后添加最小权限、发布版本并等待审批。权限新增后必须重新 OAuth，旧令牌不会自动获得新增权限。

### 5.4 获取用户 OAuth 凭证

晨报必须使用 **user access token**，不能使用 tenant access token 或 bot 身份替代。推荐由 Agent 使用浏览器和本机临时回调服务完成标准 OAuth：

1. 在进程内安全读取 App Secret，不向任何输出通道打印。
2. 在 `127.0.0.1:3000` 启动临时 HTTP 服务，只处理 `/callback`。
3. 构造授权地址，scope 使用第 3 节的最小权限集合。
4. 在用户确认后打开授权页并点击“授权”。
5. 回调收到 `code` 后，立即在进程内换取 token；不要输出 `code`。
6. 用 access token 调用用户信息接口取得当前用户 `open_id`。
7. token 成功换取后立即关闭临时回调服务并清理进程内 App Secret、授权码和 token 变量。

授权入口：

```text
GET https://open.feishu.cn/open-apis/authen/v1/authorize
```

必要参数：

```text
app_id=cli_aa0fb31596f95cb3
redirect_uri=http://localhost:3000/callback
scope=<空格分隔的最小权限>
state=<随机且需要校验的值>
```

授权码换票：

```text
POST https://open.feishu.cn/open-apis/authen/v2/oauth/token
Content-Type: application/json; charset=utf-8
```

请求体字段：

```json
{
  "grant_type": "authorization_code",
  "client_id": "cli_aa0fb31596f95cb3",
  "client_secret": "<仅保存在进程内>",
  "code": "<仅保存在进程内>",
  "redirect_uri": "http://localhost:3000/callback"
}
```

获取当前用户：

```text
GET https://open.feishu.cn/open-apis/authen/v1/user_info
Authorization: Bearer <仅保存在进程内>
```

本项目不从桌面端或其他 CLI 凭证库读取令牌。授权完成后，只将供 Server 使用的用户令牌写入下节的 `.env`。

### 5.5 写入本地凭证

目标文件：

```text
server/main/xiaozhi-server/data/.env
```

内容：

```dotenv
FEISHU_USER_ACCESS_TOKEN=<当前用户的 user access token>
FEISHU_SELF_OPEN_ID=<当前用户的 open_id>
```

使用不会把值回显到终端的方式，从进程内存直接写入文件。写完后：

```bash
chmod 600 data/.env
git check-ignore -v data/.env
stat -f '%Sp %N' data/.env    # macOS
```

Linux 可使用：

```bash
stat -c '%A %n' data/.env
```

预期权限为 `-rw-------`。验证时禁止输出文件内容。

### 5.6 启用晨报

编辑被 Git 忽略的 `server/main/xiaozhi-server/data/.config.yaml`。保留已有内容，只新增或合并以下字段：

```yaml
morning_brief:
  enabled: true
  base_url: https://open.feishu.cn
  calendar_enabled: true
  timezone: Asia/Shanghai
  ledger_path: data/morning_brief.sqlite3
```

如果没有日历权限，将 `calendar_enabled` 改为 `false`。不要把 App Secret 或任何 token 写进 YAML。

## 6. 真实验收

在 Server 目录执行：

```bash
.venv/bin/python run_morning_brief_check.py
```

Agent 必须读取完整输出并逐项对照第 1 节完成标准。不能只以进程退出码为依据，也不能因为 `/health` 为 `READY` 就跳过 `/preview`；首次预览前 `READY` 只说明本地配置齐全，不代表外部权限一定可用。

完成真实检查后，再运行聚焦测试：

```bash
.venv/bin/python -m pytest tests/test_morning_brief_*.py -q
.venv/bin/python -m compileall -q core/morning_brief run_morning_brief_check.py
git diff --check
```

真实返回可能包含用户消息摘要。Agent 只向用户报告覆盖状态、条数和错误，不应在最终答复复述私人消息正文。

## 7. 错误分流

| 现象或错误码 | Agent 处理方式 |
| --- | --- |
| `20010` | 当前用户没有应用使用权限；请管理员把用户加入应用可用范围。 |
| `20029` | 回调 URL 不匹配；检查协议、端口、路径是否与安全设置完全一致。 |
| `20043` | scope 未开通、未发布或名称错误；核对权限并重新发版。 |
| `99991668` | token 缺失、失效或过期；重新 OAuth。 |
| `99991679` | 用户令牌缺少权限；若刚新增权限，重新 OAuth 获取新令牌。 |
| `230027` | 缺少单聊或群聊的 `get_as_user` 补充权限。 |
| `99992351` | `FEISHU_SELF_OPEN_ID` 不是有效 `open_id`；重新调用 `user_info` 获取。 |
| `coverage_status=PARTIAL` | 查看具体 source 的 `error`，只修复失败数据源，不要盲目扩权。 |
| 日历权限无法提供 | 将 `calendar_enabled` 设为 `false`，重新验证消息和提及源。 |
| token 每约两小时过期 | 当前实现需要重新 OAuth；不得宣称支持长期无人值守。 |

## 8. 自动续期边界

飞书 access token 的实际有效期以 OAuth 响应的 `expires_in` 为准，常见值约为 7200 秒。获取 refresh token 需要开通并授权 `offline_access`，刷新接口为：

```text
POST https://accounts.feishu.cn/oauth/v3/token
```

refresh token 只能使用一次，刷新后必须原子替换 access token 和 refresh token。当前仓库没有实现到期时间存储、并发刷新锁和凭证原子轮换，因此 Agent 遇到“每天无人值守运行”的要求时，应明确报告这一代码缺口，再按仓库开发流程实现，不能只修改 env 或定时重跑旧 refresh token。

## 9. 最终交付格式

Agent 完成后只需要向用户报告：

- 复用的应用名称和 App ID；
- OAuth 用户姓名，但不报告 `open_id` 全值；
- `.env` 已写入、已被 Git 忽略、权限为 `0600`；
- 晨报启用状态；
- `health`、`preview`、`latest` 的 HTTP 状态；
- 三个数据源的覆盖状态和条数；
- 测试、编译和 `git diff --check` 结果；
- 当前 token 是否需要后续重新授权或是否已实现自动续期。

禁止在最终交付中包含 App Secret、授权码、access token、refresh token、完整 `open_id` 或私人消息正文。
