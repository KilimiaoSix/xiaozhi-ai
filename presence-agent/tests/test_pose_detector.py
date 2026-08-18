from presence_agent.pose_detector import Landmark, PoseObservation


def landmark(visibility=0.0):
    return Landmark(x=0.0, y=0.0, z=0.0, visibility=visibility, presence=visibility)


def pose_with_visible(indices):
    landmarks = [landmark() for _ in range(33)]
    for index in indices:
        landmarks[index] = landmark(0.8)
    return PoseObservation(tuple(landmarks))


def test_empty_pose_is_not_present():
    assert PoseObservation(()).is_present() is False


def test_four_visible_core_points_require_a_shoulder():
    assert pose_with_visible({0, 13, 14, 23}).is_present() is False
    assert pose_with_visible({0, 11, 13, 23}).is_present() is True


def test_visibility_threshold_is_inclusive():
    pose = pose_with_visible({0, 11, 13})
    landmarks = list(pose.landmarks)
    landmarks[23] = landmark(0.5)

    assert PoseObservation(tuple(landmarks)).is_present(min_confidence=0.5) is True


def test_diagnostic_metrics_only_contains_aggregate_counts():
    pose = pose_with_visible({0, 11, 13, 23, 24})

    assert pose.diagnostic_metrics() == {
        "visible_core_landmarks": 5,
        "has_visible_shoulder": True,
    }
