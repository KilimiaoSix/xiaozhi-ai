"""创建飞书任务与会议工作台依赖。"""

from core.feishu_workspace.service import FeishuWorkspaceService
from core.morning_brief.factory import (
    create_feishu_client,
    load_feishu_user_settings,
)


def create_feishu_workspace_service(config: dict) -> FeishuWorkspaceService:
    brief_config, _, self_open_id = load_feishu_user_settings(config)
    return FeishuWorkspaceService(
        create_feishu_client(config),
        self_open_id,
        timezone_name=str(brief_config.get("timezone", "Asia/Shanghai")),
    )
