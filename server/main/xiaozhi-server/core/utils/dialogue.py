import uuid
import re
from typing import List, Dict
from datetime import datetime


class Message:
    def __init__(
            self,
            role: str,
            content: str = None,
            uniq_id: str = None,
            tool_calls=None,
            tool_call_id=None,
            is_temporary=False,
    ):
        self.uniq_id = uniq_id if uniq_id is not None else str(uuid.uuid4())
        self.role = role
        self.content = content
        self.tool_calls = tool_calls
        self.tool_call_id = tool_call_id
        self.is_temporary = is_temporary  # 标记临时消息（如工具调用提醒）


# 送给大模型的历史消息条数上限（不含 system 与 few-shot）。
# 设备改为常连之后 ConnectionHandler 不再随对话结束销毁，历史会累积一整天，
# 每轮全量重发最终会撞 token 上限报 400。40 条约等于 20 轮问答。
DEFAULT_MAX_HISTORY_MESSAGES = 40


class Dialogue:
    def __init__(self, max_history_messages: int = DEFAULT_MAX_HISTORY_MESSAGES):
        self.dialogue: List[Message] = []
        # 0 或负数表示不截断
        self.max_history_messages = max_history_messages
        # 获取当前时间
        self.current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _truncate_history(self, messages: List[Message]) -> List[Message]:
        """只保留最近若干条实际对话。

        截断可能把 assistant 的 tool_calls 切掉而留下孤儿 tool 消息，
        这种消息发给大模型会直接 400，所以从头丢弃直到第一条非 tool 消息。
        尾部的悬空 tool_calls 由 _ensure_tool_calls_complete 补齐。
        """
        limit = self.max_history_messages
        if not limit or limit <= 0 or len(messages) <= limit:
            return messages

        kept = messages[-limit:]
        while kept and kept[0].role == "tool":
            kept.pop(0)
        return kept

    def put(self, message: Message):
        self.dialogue.append(message)

    def getMessages(self, m, dialogue):
        if m.tool_calls is not None:
            dialogue.append({"role": m.role, "tool_calls": m.tool_calls})
        elif m.role == "tool":
            dialogue.append(
                {
                    "role": m.role,
                    "tool_call_id": (
                        str(uuid.uuid4()) if m.tool_call_id is None else m.tool_call_id
                    ),
                    "content": m.content,
                }
            )
        else:
            dialogue.append({"role": m.role, "content": m.content})

    def get_llm_dialogue(self) -> List[Dict[str, str]]:
        # 直接调用get_llm_dialogue_with_memory，传入None作为memory_str
        # 这样确保说话人功能在所有调用路径下都生效
        return self.get_llm_dialogue_with_memory(None, None)

    def update_system_message(self, new_content: str):
        """更新或添加系统消息"""
        # 查找第一个系统消息
        system_msg = next((msg for msg in self.dialogue if msg.role == "system"), None)
        if system_msg:
            system_msg.content = new_content
        else:
            self.put(Message(role="system", content=new_content))

    def _ensure_tool_calls_complete(self, messages: List[Message]) -> List[Message]:
        """
        确保所有 tool_calls 都有对应的 tool 响应
        修复被打断导致的悬空 tool_calls，防止大模型 API 报 400 错误
        """
        pending_tool_calls = set()
        result = []

        for msg in messages:
            result.append(msg)

            if msg.role == "assistant" and msg.tool_calls:
                for tc in msg.tool_calls:
                    tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
                    if tc_id:
                        pending_tool_calls.add(tc_id)

            elif msg.role == "tool" and msg.tool_call_id:
                pending_tool_calls.discard(msg.tool_call_id)

        for missing_id in pending_tool_calls:
            dummy_tool_msg = Message(
                role="tool",
                content='{"status": "interrupted", "message": "动作已取消/被打断"}',
                tool_call_id=missing_id
            )
            result.append(dummy_tool_msg)

        return result

    def get_llm_dialogue_with_memory(
            self, memory_str: str = None, voiceprint_config: dict = None,
            current_speaker: str = None,
    ) -> List[Dict[str, str]]:
        # 构建对话
        dialogue = []

        # 添加系统提示和记忆
        system_message = next(
            (msg for msg in self.dialogue if msg.role == "system"), None
        )

        if system_message:
            full_prompt = system_message.content

            # 替换时间占位符
            full_prompt = full_prompt.replace(
                "{{current_time}}", datetime.now().strftime("%H:%M")
            )

            # 填充记忆
            if memory_str is not None:
                full_prompt = re.sub(
                    r"<memory>.*?</memory>",
                    f"<memory>\n{memory_str}\n</memory>",
                    full_prompt,
                    flags=re.DOTALL,
                )

            # 追加说话人信息
            try:
                current_speaker_name = (current_speaker or "").strip()
                # 仅在本轮注入了有效身份时才输出 speakers_info，避免列表里的名字每轮
                # 重复出现诱导模型反复称呼；后续轮不再注入身份，靠对话历史首轮保留
                if current_speaker_name and current_speaker_name != "未知说话人":
                    speakers = voiceprint_config.get("speakers", [])
                    speakers_info = "\n<speakers_info>"
                    speakers_info += f"\n当前说话人：{current_speaker_name}"
                    for speaker_str in speakers:
                        try:
                            parts = speaker_str.split(",", 2)
                            if len(parts) >= 2:
                                name = parts[1].strip()
                                description = (
                                    parts[2].strip() if len(parts) >= 3 else ""
                                )
                                speakers_info += f"\n- {name}：{description}"
                        except:
                            pass
                    speakers_info += "\n</speakers_info>"
                    full_prompt += speakers_info
            except:
                pass

            dialogue.append({"role": "system", "content": full_prompt})

        # 第二段：few-shot 示例（会话内不变）
        non_system_messages = [m for m in self.dialogue if m.role != "system"]
        fewshot_messages = [m for m in non_system_messages if m.is_temporary]
        complete_fewshot = self._ensure_tool_calls_complete(fewshot_messages)
        for m in complete_fewshot:
            self.getMessages(m, dialogue)

        # 第三段：实际对话历史（不含 few-shot）
        actual_messages = [m for m in non_system_messages if not m.is_temporary]
        actual_messages = self._truncate_history(actual_messages)
        complete_actual = self._ensure_tool_calls_complete(actual_messages)
        for m in complete_actual:
            self.getMessages(m, dialogue)

        return dialogue
