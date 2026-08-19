"""服务端无头跑 claude 做只读故障诊断（需求文档流程七）。

主人一句「启动诊断」，服务端起一个 `claude -p` 子进程去看日志与配置，
把结论念回来。三条硬约束：

1. **只读**：只给 Read/Grep/Glob 这类只读工具（--allowedTools），诊断进程
   不允许改任何东西。生产操作必须人来做，机器人只提供事实与建议。
2. **有界**：子进程超时（默认 300 秒）强杀。诊断卡住时最坏结果是「诊断没跑完」
   一句话，而不是一个永远挂着的僵尸进程占着事件循环里的任务槽。
3. **不吃服务端的 LLM 密钥**：子进程环境里剔掉 ANTHROPIC_API_KEY。服务端自己的
   LLM provider 可能把别的 key 塞在这个变量名下（配置里 LLM 是可切换的），
   claude CLI 会优先用环境变量里的 key 而不是本机已登录的凭据，带着它跑必然
   401。剔掉之后 CLI 走本机登录态。

`claude -p ... --output-format json` 的实测输出结构（claude CLI 2.1.229，macOS，
2026-08-19 实跑 `claude -p "说'诊断链路自检通过'这七个字" --output-format json`）：

    {"type":"result","subtype":"success","is_error":false,
     "result":"诊断链路自检通过",
     "session_id":"d961ba88-...","num_turns":1,"stop_reason":"end_turn",
     "duration_ms":8255,"duration_api_ms":5876,"total_cost_usd":0.2516,
     "usage":{...},"modelUsage":{...},"permission_denials":[],
     "terminal_reason":"completed","api_error_status":null,"uuid":"..."}

也就是：单个顶层 JSON 对象，正文在 `result`，成败看 `is_error`（失败时
subtype 不是 "success"）。本模块按这个结构解析，并对「不是 JSON」「没有 result」
两种畸形输出都给出可播报的失败原因。

告警文案来自外部 webhook，是不可信输入，会原样进 prompt：因此只给只读工具、
并且截断长度；诊断结论只用于播报，永远不驱动任何动作。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Callable, Optional

TAG = __name__

DEFAULT_CLAUDE_BIN = "claude"
DEFAULT_ALLOWED_TOOLS = "Read Grep Glob"
DEFAULT_TIMEOUT_S = 300.0

# 播报用的结论长度上限。prompt 已经要求三句话以内，这里只是兜底：
# 模型偶尔会写长，念上两分钟比不念还糟。
MAX_SUMMARY_CHARS = 300
# 告警字段进 prompt 前的截断长度，防止一条超长 message 把 prompt 撑爆
MAX_FIELD_CHARS = 500
# stderr 只取尾部进错误文案，避免把几百行栈念出来
MAX_STDERR_CHARS = 300

PROMPT_TEMPLATE = """你是故障诊断助手，只做只读分析：检查该目录可用的日志与配置，给出 1)最可能根因 2)关联的最近变更 3)建议的下一步动作；三句话以内，中文，不要执行任何修改。

告警上下文：
- 服务：{service}
- 严重度：{severity}
- 标题：{title}
- 详情：{message}
- 指标：{metric}
- 当前值：{value}
- 开始时间：{started_at}
- 重复上报：{repeat_count} 次
{simulated_note}"""

SIMULATED_NOTE = "- 注意：这是一条用于演示的模拟告警\n"


def _truncate(value: Any, limit: int) -> str:
    text = str(value if value is not None else "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _normalize_summary(text: str) -> str:
    """把结论压成一行：TTS 遇到换行会断句断得很奇怪。"""
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= MAX_SUMMARY_CHARS:
        return collapsed
    return collapsed[:MAX_SUMMARY_CHARS] + "…"


def _allowed_tools_arg(value: Any) -> str:
    """配置可以写字符串 "Read Grep Glob" 或列表 ["Read","Grep"]，都归一成一个参数。"""
    if isinstance(value, (list, tuple, set)):
        joined = " ".join(str(item).strip() for item in value if str(item).strip())
        return joined or DEFAULT_ALLOWED_TOOLS
    text = str(value or "").strip()
    return text or DEFAULT_ALLOWED_TOOLS


async def _default_spawn(argv, cwd, env):
    """真正起子进程。

    stdin 关成 DEVNULL：claude 在非交互模式下若发现 stdin 是管道会等输入，
    服务端没人喂它，就会一直挂到超时。
    """
    return await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


class DiagnosisRunner:
    """跑一次只读诊断，返回 {ok, summary, raw?, error?}。

    spawn 可注入：单测用假子进程覆盖成功 / 超时 / 非零退出 / 坏 JSON 四条路径，
    不真的调用 claude（真跑一次要几十秒且花钱）。
    """

    def __init__(
        self,
        config: Optional[dict] = None,
        *,
        spawn: Optional[Callable] = None,
        on_result: Optional[Callable] = None,
        logger=None,
    ) -> None:
        section = (config or {}).get("incident") or {}
        if not isinstance(section, dict):
            section = {}
        self._claude_bin = str(section.get("claude_bin") or DEFAULT_CLAUDE_BIN)
        self._diagnosis_dir = str(section.get("diagnosis_dir") or os.getcwd())
        self._allowed_tools = _allowed_tools_arg(section.get("allowed_tools"))
        # 诊断子进程默认吃 claude CLI 的全局默认模型。2026-08-19 演示中途撞上
        # 「You've reached your Fable 5 limit」(HTTP 429)，整条诊断哑掉——主力模型
        # 的额度不该把故障诊断一起拖死。留空则不传 --model，行为与接入前一致。
        self._claude_model = str(section.get("claude_model") or "").strip()
        try:
            timeout = float(section.get("timeout_s", DEFAULT_TIMEOUT_S))
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT_S
        self._timeout_s = timeout if timeout > 0 else DEFAULT_TIMEOUT_S
        self._spawn = spawn or _default_spawn
        self._on_result = on_result
        self._logger = logger or logging.getLogger(__name__)

    # ------------------------------------------------------------ 对外

    async def run(self, incident: dict, on_result: Optional[Callable] = None) -> dict:
        """跑一次诊断。永不抛异常——失败也要变成一句能播报的话。

        on_result 由 manager 注入，签名 (incident_id, result_dict)；
        构造时传的那个作为默认值。
        """
        incident = incident or {}
        incident_id = str(incident.get("incident_id") or "")
        result = await self._run_once(incident)

        callback = on_result or self._on_result
        if callback is not None:
            outcome = callback(incident_id, result)
            if asyncio.iscoroutine(outcome) or isinstance(outcome, asyncio.Future):
                await outcome
        return result

    def build_prompt(self, incident: dict) -> str:
        incident = incident or {}
        return PROMPT_TEMPLATE.format(
            service=_truncate(incident.get("service"), MAX_FIELD_CHARS) or "未知服务",
            severity=_truncate(incident.get("severity"), 8) or "未知",
            title=_truncate(incident.get("title"), MAX_FIELD_CHARS) or "未知告警",
            message=_truncate(incident.get("message"), MAX_FIELD_CHARS) or "（无）",
            metric=_truncate(incident.get("metric"), MAX_FIELD_CHARS) or "（无）",
            value=_truncate(incident.get("value"), MAX_FIELD_CHARS) or "（无）",
            started_at=_truncate(incident.get("started_at"), 64) or "（未提供）",
            repeat_count=int(incident.get("repeat_count") or 1),
            simulated_note=SIMULATED_NOTE if incident.get("simulated") else "",
        )

    def build_argv(self, prompt: str) -> list:
        argv = [
            self._claude_bin,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--allowedTools",
            self._allowed_tools,
        ]
        if self._claude_model:
            argv += ["--model", self._claude_model]
        return argv

    # ------------------------------------------------------------ 内部

    def _child_env(self) -> dict:
        env = dict(os.environ)
        # 见模块头注释：带着服务端 LLM 的 key 跑 claude 必然认证失败
        env.pop("ANTHROPIC_API_KEY", None)
        return env

    async def _run_once(self, incident: dict) -> dict:
        prompt = self.build_prompt(incident)
        argv = self.build_argv(prompt)

        try:
            process = await self._spawn(argv, self._diagnosis_dir, self._child_env())
        except FileNotFoundError:
            return self._fail(f"找不到 claude 命令（{self._claude_bin}）")
        except Exception as e:
            return self._fail(f"诊断进程启动失败：{e}")

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self._timeout_s
            )
        except asyncio.TimeoutError:
            await self._kill(process)
            return self._fail(f"诊断超时，已停在 {int(self._timeout_s)} 秒")
        except Exception as e:
            await self._kill(process)
            return self._fail(f"诊断进程异常：{e}")

        returncode = getattr(process, "returncode", None)
        stdout_text = _decode(stdout)
        stderr_text = _decode(stderr)

        if returncode not in (0, None):
            detail = _normalize_summary(stderr_text[-MAX_STDERR_CHARS:]) or "没有错误输出"
            return self._fail(f"claude 退出码 {returncode}，{detail}")

        return self._parse(stdout_text, stderr_text)

    async def _kill(self, process) -> None:
        """超时后强杀，并等它真的死掉。

        只 kill 不 wait 会留下僵尸进程（父进程没收尸），演示当天连跑几次就
        堆一串。这里给 5 秒收尸窗口，超了只记日志——比卡住主流程强。
        """
        try:
            process.kill()
        except ProcessLookupError:
            return
        except Exception as e:
            self._logger.warning(f"强杀诊断进程失败: {e}")
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except Exception:
            self._logger.warning("诊断进程强杀后未在 5 秒内退出")

    def _parse(self, stdout_text: str, stderr_text: str) -> dict:
        text = (stdout_text or "").strip()
        if not text:
            detail = _normalize_summary(stderr_text[-MAX_STDERR_CHARS:])
            return self._fail(f"诊断没有输出{('，' + detail) if detail else ''}")

        try:
            payload = json.loads(text)
        except (ValueError, TypeError):
            # CLI 换了输出格式、或者前面混进了非 JSON 的告警行
            return self._fail("诊断输出不是合法 JSON，无法解析")

        if not isinstance(payload, dict):
            return self._fail("诊断输出结构不认识，无法解析")

        summary = _normalize_summary(payload.get("result") or "")

        if payload.get("is_error") or payload.get("subtype") not in (None, "success"):
            reason = summary or str(payload.get("subtype") or "未知错误")
            return self._fail(f"claude 报错：{reason}", raw=payload)

        if not summary:
            return self._fail("诊断没有给出结论", raw=payload)

        return {"ok": True, "summary": summary, "raw": payload, "error": ""}

    def _fail(self, error: str, raw: Any = None) -> dict:
        self._logger.warning(f"故障诊断失败: {error}")
        return {"ok": False, "summary": "", "raw": raw, "error": error}


def _decode(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
