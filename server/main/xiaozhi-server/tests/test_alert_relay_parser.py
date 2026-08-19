from core.alert_relay.parser import DEFAULT_CLUSTER_MAP, parse_alert

FULL_ALERT = """【SAE告警通知】
告警等级：严重
告警集群：bj-jxq-autocar
命名空间：iflyplot
告警对象：iflyplot-ai-7d9f8b6c5d-x2k9p
告警规则：日志包含关键词 无痕改字处理超时 >5条
告警时间：2026-08-18 21:00:11
告警策略链接：https://one.iflytek.com/sae/#/alarm?projectId=117&clusterId=3
"""


def test_parses_every_field_of_a_real_alert():
    event = parse_alert(FULL_ALERT)
    assert event.level == "严重"
    assert event.cluster == "bj-jxq-autocar"
    assert event.namespace == "iflyplot"
    assert event.target == "iflyplot-ai-7d9f8b6c5d-x2k9p"
    assert event.workload == "iflyplot-ai"
    assert event.keyword == "无痕改字处理超时"
    assert event.alert_time == "2026-08-18 21:00:11"
    assert event.policy_url.startswith("https://one.iflytek.com/sae/")


def test_maps_known_cluster_to_sae_ids():
    """集群名 → projectId/clusterId 是拉日志的前置，映射不上就查不了。"""
    event = parse_alert(FULL_ALERT)
    assert (event.project_id, event.cluster_id) == ("117", "3")
    assert DEFAULT_CLUSTER_MAP["bj-jxq-autocar"] == ("117", "3")


def test_falls_back_to_ids_in_the_policy_url_for_unknown_clusters():
    """skill 里写明未知集群从告警策略链接的 query 里取 id，这里照抄同一条规则。"""
    text = FULL_ALERT.replace("bj-jxq-autocar", "sh-new-cluster")
    event = parse_alert(text)
    assert event.cluster == "sh-new-cluster"
    assert (event.project_id, event.cluster_id) == ("117", "3")


def test_unknown_cluster_without_url_leaves_ids_empty_instead_of_guessing():
    text = "告警集群：sh-new-cluster\n告警对象：foo-abc123def4-x1y2z\n告警规则：包含关键词 崩溃 >1条"
    event = parse_alert(text)
    assert event.project_id == ""
    assert event.cluster_id == ""


def test_accepts_halfwidth_colon_and_extra_spaces():
    text = "告警等级:  紧急\n告警集群 : bj-jxq-autocar\n告警对象: iflyplot-ai-7d9f8b6c5d-x2k9p"
    event = parse_alert(text)
    assert event.level == "紧急"
    assert event.cluster == "bj-jxq-autocar"
    assert event.workload == "iflyplot-ai"


def test_extracts_keyword_from_several_rule_phrasings():
    for rule, expected in [
        ("日志包含关键词 无痕改字处理超时 >5条", "无痕改字处理超时"),
        ("包含关键词“并发泄漏”超过 3 条", "并发泄漏"),
        ('包含关键词 "OOM killed" > 1 条', "OOM killed"),
    ]:
        event = parse_alert(f"告警规则：{rule}")
        assert event.keyword == expected, rule


def test_keeps_raw_text_verbatim_for_the_diagnosis_prompt():
    """诊断子进程要吃原文，解析只是给通知用的摘要，不能替代原文。"""
    event = parse_alert(FULL_ALERT)
    assert event.raw_text == FULL_ALERT


def test_defaults_to_warning_level_when_absent():
    event = parse_alert("告警集群：bj-jxq-autocar")
    assert event.level == "警告"


def test_normalizes_level_aliases():
    assert parse_alert("告警等级：critical").level == "严重"
    assert parse_alert("告警等级：P0").level == "紧急"
    assert parse_alert("告警等级：warning").level == "警告"


def test_explicit_overrides_win_over_parsed_text():
    """调用方明确给了字段就以它为准，解析只做兜底。"""
    event = parse_alert(FULL_ALERT, overrides={"keyword": "并发泄漏", "namespace": "iflyplot-pre"})
    assert event.keyword == "并发泄漏"
    assert event.namespace == "iflyplot-pre"
    assert event.cluster == "bj-jxq-autocar"


def test_blank_overrides_do_not_erase_parsed_values():
    event = parse_alert(FULL_ALERT, overrides={"keyword": "", "namespace": None})
    assert event.keyword == "无痕改字处理超时"
    assert event.namespace == "iflyplot"


def test_custom_cluster_map_extends_the_builtin_one():
    event = parse_alert(
        FULL_ALERT.replace("bj-jxq-autocar", "hf-lab").replace(
            "告警策略链接：https://one.iflytek.com/sae/#/alarm?projectId=117&clusterId=3", ""
        ),
        cluster_map={"hf-lab": ("902", "7")},
    )
    assert (event.project_id, event.cluster_id) == ("902", "7")


def test_statefulset_pod_suffix_is_stripped():
    event = parse_alert("告警对象：iflyplot-mysql-0")
    assert event.workload == "iflyplot-mysql"


def test_random_suffix_length_is_tolerated():
    """推导失败不只是拉不到日志，还会毁掉去重指纹——每个 pod 都算一条新告警。"""
    for pod in (
        "iflyplot-ai-7d9f8b6c5d-x2k9p",   # k8s 标准 5 位
        "iflyplot-ai-7d9f8b6c5d-x2k9",    # 4 位
        "iflyplot-ai-7d9f8b6c5d-x2k9pz",  # 6 位
    ):
        assert parse_alert(f"告警对象：{pod}").workload == "iflyplot-ai", pod


def test_a_plain_workload_name_is_left_alone():
    """告警对象有时直接给的就是 workload，不能被当成 pod 削掉一截。"""
    assert parse_alert("告警对象：iflyplot-ai").workload == "iflyplot-ai"
