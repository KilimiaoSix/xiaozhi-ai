from core.feishu_workspace.factory import create_feishu_workspace_service


def test_factory_reuses_server_feishu_user_credentials(monkeypatch):
    monkeypatch.setenv("FEISHU_USER_ACCESS_TOKEN", "u-workspace-token")
    monkeypatch.setenv("FEISHU_SELF_OPEN_ID", "ou_workspace")

    service = create_feishu_workspace_service(
        {
            "morning_brief": {
                "base_url": "https://open.feishu.cn",
                "timezone": "Asia/Shanghai",
                "page_size": 50,
                "max_pages": 12,
                "timeout_seconds": 9,
            }
        }
    )

    assert service.status()["state"] == "ready"
    assert service.status()["open_id"] == "ou_workspace"
    assert service.client.max_pages == 12
    assert service.client.timeout_seconds == 9
