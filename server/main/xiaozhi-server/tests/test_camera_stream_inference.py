from dataclasses import dataclass

from core.camera_stream.inference import CameraInferenceRuntime


FRAME = object()


class FakeDecoder:
    def __init__(self):
        self.calls = []

    def __call__(self, jpeg):
        self.calls.append(jpeg)
        return FRAME


class FakePoseObservation:
    def is_present(self):
        return True

    def diagnostic_metrics(self):
        return {"visible_core_landmarks": 5, "has_visible_shoulder": True}


class FakePoseDetector:
    def __init__(self):
        self.calls = []
        self.closed = False

    def detect(self, frame, timestamp_ms):
        self.calls.append((frame, timestamp_ms))
        return FakePoseObservation()

    def close(self):
        self.closed = True


@dataclass
class FakeState:
    value: str


class FakePresenceTracker:
    def __init__(self):
        self.state = FakeState("starting")
        self.calls = []

    def update(self, detected, now):
        self.calls.append((detected, now))
        self.state = FakeState("present")
        return self.state

    def metrics(self, now):
        return {"positive_streak": 3, "seconds_since_last_positive": 0.0}


class FakeIdentity:
    def to_payload(self):
        return {
            "state": "owner",
            "previous_state": "starting",
            "changed": True,
            "face_count": 1,
            "similarity": 0.731245,
        }


class FakeFaceVerifier:
    def __init__(self):
        self.frames = []

    def observe(self, frame, now):
        self.frames.append((frame, now))
        return FakeIdentity()


def test_decodes_once_and_runs_pose_and_face_on_the_same_frame():
    decoder = FakeDecoder()
    pose = FakePoseDetector()
    tracker = FakePresenceTracker()
    face = FakeFaceVerifier()
    runtime = CameraInferenceRuntime(
        decoder=decoder,
        pose_detector=pose,
        presence_tracker=tracker,
        face_verifier=face,
        face_threshold=0.45,
    )

    result = runtime.process(b"jpeg", sequence=7, now=12.5)

    assert decoder.calls == [b"jpeg"]
    assert pose.calls[0][0] is FRAME
    assert face.frames == [(FRAME, 12.5)]
    assert result["sequence"] == 7
    assert result["presence"] == {"state": "present", "changed": True}
    assert result["identity"] == {
        "state": "owner",
        "previous_state": "starting",
        "changed": True,
        "face_count": 1,
        "face_detected": True,
        "similarity": 0.731245,
        "threshold": 0.45,
        "matched": True,
    }
    assert result["metrics"]["visible_core_landmarks"] == 5
    assert result["metrics"]["positive_streak"] == 3


class NoFaceIdentity:
    def to_payload(self):
        return {
            "state": "no_face",
            "previous_state": "owner",
            "changed": True,
            "face_count": 0,
        }


class NoFaceVerifier:
    def observe(self, frame, now):
        return NoFaceIdentity()


def test_non_single_face_result_does_not_reuse_similarity():
    runtime = CameraInferenceRuntime(
        decoder=lambda _: FRAME,
        pose_detector=FakePoseDetector(),
        presence_tracker=FakePresenceTracker(),
        face_verifier=NoFaceVerifier(),
    )

    identity = runtime.process(b"jpeg", sequence=1, now=1.0)["identity"]

    assert identity["face_detected"] is False
    assert identity["matched"] is False
    assert "similarity" not in identity
    assert "threshold" not in identity


def test_close_releases_pose_detector():
    pose = FakePoseDetector()
    runtime = CameraInferenceRuntime(
        decoder=lambda _: FRAME,
        pose_detector=pose,
        presence_tracker=FakePresenceTracker(),
        face_verifier=NoFaceVerifier(),
    )

    runtime.close()

    assert pose.closed is True


# ------------------------------------------------------ 人脸即在岗（face_implies_present）
#
# pose_detector.is_present() 要求 7 个核心关键点里可见 ≥4 个且含肩膀。坐得离摄像头
# 很近时画面里只有头和肩顶，关键点常年停在 3 个，姿态恒判 absent——人明明在工位，
# 编排收不到 present，到岗迎接与返岗汇总全都不会触发（8-19 实测：脸稳定认出 owner
# 相似度 0.53~0.72，presence 却连续 20 秒 absent）。
# 打开这个开关后，看到人脸也算在岗，姿态与人脸取或。


class ConfigurablePose:
    def __init__(self, present: bool) -> None:
        self._present = present

    def is_present(self):
        return self._present

    def diagnostic_metrics(self):
        return {"visible_core_landmarks": 3, "has_visible_shoulder": True}


class ConfigurablePoseDetector:
    def __init__(self, present: bool) -> None:
        self._present = present

    def detect(self, frame, timestamp_ms):
        return ConfigurablePose(self._present)

    def close(self):
        pass


class ConfigurableFaceVerifier:
    def __init__(self, face_count: int) -> None:
        self._face_count = face_count

    def observe(self, frame, now):
        class _Identity:
            def to_payload(_self):
                return {"state": "owner", "face_count": self._face_count}

        return _Identity()


def _runtime(*, pose_present, face_count, face_implies_present):
    tracker = FakePresenceTracker()
    runtime = CameraInferenceRuntime(
        decoder=FakeDecoder(),
        pose_detector=ConfigurablePoseDetector(pose_present),
        presence_tracker=tracker,
        face_verifier=ConfigurableFaceVerifier(face_count),
        face_threshold=0.45,
        face_implies_present=face_implies_present,
    )
    runtime.process(b"jpeg", sequence=1, now=1.0)
    return tracker.calls[0][0]


def test_face_alone_is_not_present_by_default():
    """默认行为不变：只看得到脸、姿态说没人，仍然判 absent。"""
    assert _runtime(pose_present=False, face_count=1, face_implies_present=False) is False


def test_face_alone_counts_as_present_when_enabled():
    assert _runtime(pose_present=False, face_count=1, face_implies_present=True) is True


def test_no_face_and_no_pose_stays_absent_when_enabled():
    """开关只是放宽在岗判定，不是恒真——画面空了照样要能判离席。"""
    assert _runtime(pose_present=False, face_count=0, face_implies_present=True) is False


def test_pose_still_wins_when_face_missing():
    """姿态看得到人、人脸没扫到（低头/背光）：仍然在岗。"""
    assert _runtime(pose_present=True, face_count=0, face_implies_present=True) is True
