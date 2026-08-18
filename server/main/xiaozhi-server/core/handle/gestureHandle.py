"""设备端手势事件。

固件（PAJ7620U2）在本地驱动完舵机动作后，会通过既有的 listen/detect 通道把手势
上报上来，文本形如 "[gesture]wave"，与 "[device_call]" 标签同一套玩法，不新增协议。

这里只负责把手势名翻译成一句可读的中文描述交给 LLM，具体"挥手=延后番茄钟"之类的
语义留给智能体的提示词决定，避免把产品策略写死在协议层。
"""
from typing import Optional

TAG = __name__

GESTURE_TAG = "[gesture]"

# 手势名必须与固件 GestureSensor::GestureName 保持一致
GESTURE_PROMPTS = {
    "left": "用户对我做了一个向左挥的手势",
    "right": "用户对我做了一个向右挥的手势",
    "up": "用户对我做了一个向上挥的手势",
    "down": "用户对我做了一个向下挥的手势",
    "forward": "用户把手靠近了我，做了一个向前推的手势",
    "backward": "用户把手移开了，做了一个向后拉的手势",
    "clockwise": "用户对我做了一个顺时针画圈的手势",
    "counter_clockwise": "用户对我做了一个逆时针画圈的手势",
    "wave": "用户对我挥了挥手",
}


def parse_gesture_text(text: str) -> Optional[str]:
    """从 detect 文本里取出手势名，不是手势事件则返回 None。"""
    if not text or not text.startswith(GESTURE_TAG):
        return None
    return text[len(GESTURE_TAG):].strip() or None


def gesture_prompt(gesture: str) -> Optional[str]:
    """手势名转中文描述，未知手势返回 None。"""
    if not gesture:
        return None
    return GESTURE_PROMPTS.get(gesture)
