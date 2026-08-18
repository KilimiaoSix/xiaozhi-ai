import asyncio
from aiohttp import web
from config.logger import setup_logging
from core.api.ota_handler import OTAHandler
from core.api.vision_handler import VisionHandler
from core.api.event_handler import EventHandler
from core.api.morning_brief_handler import MorningBriefHandler
from core.api.presence_handler import PresenceHandler
from core.api.camera_stream_handler import CameraStreamHandler
from core.api.alert_relay_handler import AlertRelayHandler
from core.alert_relay.factory import create_alert_relay_service
from core.alert_relay_routes import add_alert_relay_routes
from core.morning_brief.factory import create_morning_brief_service
from core.morning_brief_routes import add_morning_brief_routes
from core.presence_registry import PresenceRegistry
from core.presence_routes import add_presence_routes

TAG = __name__


class SimpleHttpServer:
    def __init__(self, config: dict, ws_server=None):
        self.config = config
        self.logger = setup_logging()
        self.ota_handler = OTAHandler(config)
        self.vision_handler = VisionHandler(config)
        # 只有拿到 WebSocket 服务实例才能定位在线设备，进而开放事件推送接口
        self.ws_server = ws_server
        self.event_handler = (
            EventHandler(config, ws_server.device_registry) if ws_server else None
        )
        self.presence_registry = PresenceRegistry()
        self.presence_handler = PresenceHandler(
            config,
            self.presence_registry,
            logger=self.logger,
        )
        self.camera_stream_handler = CameraStreamHandler(
            config,
            self.presence_registry,
            logger=self.logger,
        )
        self.morning_brief_service = create_morning_brief_service(config)
        self.morning_brief_handler = MorningBriefHandler(
            config,
            self.morning_brief_service,
            logger=self.logger,
        )
        # 告警值班中继：机器人 + 飞书叫人，人点头后调起本机 Claude Code 排查。
        # 没有 ws_server 就没有 device_registry，硬件那一路会自行降级为不可用。
        self.alert_relay_service = create_alert_relay_service(
            config,
            device_registry=ws_server.device_registry if ws_server else None,
            logger=self.logger,
        )
        self.alert_relay_handler = AlertRelayHandler(
            config,
            self.alert_relay_service,
            logger=self.logger,
        )

    def _get_websocket_url(self, local_ip: str, port: int) -> str:
        """获取websocket地址

        Args:
            local_ip: 本地IP地址
            port: 端口号

        Returns:
            str: websocket地址
        """
        server_config = self.config["server"]
        websocket_config = server_config.get("websocket")

        if websocket_config and "你" not in websocket_config:
            return websocket_config
        else:
            return f"ws://{local_ip}:{port}/xiaozhi/v1/"

    def create_app(self) -> web.Application:
        app = web.Application()
        add_presence_routes(
            app,
            self.presence_handler,
            self.camera_stream_handler,
        )
        add_morning_brief_routes(app, self.morning_brief_handler)
        add_alert_relay_routes(app, self.alert_relay_handler)

        if not self.config.get("read_config_from_api", False):
            # 单模块运行时开放 OTA 和固件下载接口。
            app.add_routes(
                [
                    web.get("/xiaozhi/ota/", self.ota_handler.handle_get),
                    web.post("/xiaozhi/ota/", self.ota_handler.handle_post),
                    web.options("/xiaozhi/ota/", self.ota_handler.handle_options),
                    web.get(
                        "/xiaozhi/ota/download/{filename}",
                        self.ota_handler.handle_download,
                    ),
                    web.options(
                        "/xiaozhi/ota/download/{filename}",
                        self.ota_handler.handle_options,
                    ),
                ]
            )
        if self.event_handler:
            # 外部工作事件推送接口（Codex / Claude Code / 告警等）
            app.add_routes(
                [
                    web.post("/xiaozhi/event/push", self.event_handler.handle_push),
                    web.get(
                        "/xiaozhi/event/devices", self.event_handler.handle_devices
                    ),
                    web.options(
                        "/xiaozhi/event/push", self.event_handler.handle_options
                    ),
                ]
            )
        app.add_routes(
            [
                web.get("/mcp/vision/explain", self.vision_handler.handle_get),
                web.post("/mcp/vision/explain", self.vision_handler.handle_post),
                web.options(
                    "/mcp/vision/explain", self.vision_handler.handle_options
                ),
            ]
        )
        return app

    async def start(self):
        try:
            server_config = self.config["server"]
            host = server_config.get("ip", "0.0.0.0")
            port = int(server_config.get("http_port", 8003))

            if port:
                app = self.create_app()

                # 运行服务
                runner = web.AppRunner(app)
                await runner.setup()
                site = web.TCPSite(runner, host, port)
                await site.start()

                # 超时未认领的告警要升级提醒，巡检必须在事件循环起来之后再挂。
                sweep_interval = float(
                    self.config.get("alert_relay", {}).get("sweep_interval_seconds", 60)
                )
                await self.alert_relay_service.start(sweep_interval)

                # 保持服务运行
                while True:
                    await asyncio.sleep(3600)  # 每隔 1 小时检查一次
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"HTTP服务器启动失败: {e}")
            import traceback

            self.logger.bind(tag=TAG).error(f"错误堆栈: {traceback.format_exc()}")
            raise
