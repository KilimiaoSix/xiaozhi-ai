"""唤醒应答后的 VAD 静默期时长。

固件播报期间本板（无 AEC）麦克风不采音，播完进聆听态后服务端还会因
just_woken_up 把音频整帧丢弃，恢复定时器从聆听态首帧起算。上游写死 2 秒，
而唤醒应答的文案（"您请说，我正听着"）恰恰在邀请用户立刻开口——
紧跟着说的短问题整句落进聋区，真机上表现为「唤醒后提问没反应，
像是麦克风关了」。静默期只需要盖住应答的混响尾音，时长必须可配。
"""

from core.handle.receiveAudioHandle import wakeup_resume_vad_seconds


def test_missing_config_falls_back_to_upstream_two_seconds():
    """没配就维持上游原值，不能因为升级悄悄改别人的聋区长度。"""
    assert wakeup_resume_vad_seconds({}) == 2.0
    assert wakeup_resume_vad_seconds(None) == 2.0


def test_configured_value_wins():
    assert wakeup_resume_vad_seconds({"wakeup_resume_vad_seconds": 0.4}) == 0.4


def test_zero_disables_the_deaf_window():
    assert wakeup_resume_vad_seconds({"wakeup_resume_vad_seconds": 0}) == 0.0


def test_garbage_values_fall_back():
    assert wakeup_resume_vad_seconds({"wakeup_resume_vad_seconds": "abc"}) == 2.0
    assert wakeup_resume_vad_seconds({"wakeup_resume_vad_seconds": -3}) == 2.0
    assert wakeup_resume_vad_seconds({"wakeup_resume_vad_seconds": None}) == 2.0
