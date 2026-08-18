"""Optional local preview rendering. No rendered data leaves this process."""

from importlib import import_module


STATE_COLORS = {
    "starting": (0, 215, 255),
    "present": (60, 200, 80),
    "absent": (80, 80, 230),
    "camera_error": (80, 80, 230),
}


def render_frame(frame, observation, state, fps):
    cv2 = import_module("cv2")
    color = STATE_COLORS[state.value]
    cv2.putText(
        frame,
        f"Presence: {state.value}",
        (20, 38),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.8,
        color,
        2,
        cv2.LINE_AA,
    )
    cv2.putText(
        frame,
        f"FPS: {fps:.1f}",
        (20, 70),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.55,
        (240, 240, 240),
        1,
        cv2.LINE_AA,
    )
    return frame
