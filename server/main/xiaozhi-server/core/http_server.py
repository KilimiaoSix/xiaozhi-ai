import asyncio
from aiohttp import web
from config.logger import setup_logging
from core.api.ota_handler import OTAHandler
from core.api.vision_handler import VisionHandler
from core.api.event_handler import EventHandler
from core.api.morning_brief_handler import MorningBriefHandler
from core.api.presence_handler import PresenceHandler
from core.api.camera_stream_handler import CameraStreamHandler
from core.morning_brief.factory import create_morning_brief_service
from core.morning_brief_routes import add_morning_brief_routes
from core.api.pomodoro_handler import PomodoroHandler
from core.presence_registry import PresenceRegistry
from core.presence_routes import add_presence_routes
from core.pomodoro_routes import add_pomodoro_routes
from core.wellbeing import WellbeingService

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
        self.wellbeing_service = (
            WellbeingService(
                config,
                self.presence_registry,
                ws_server.device_registry,
                logger=self.logger,
            )
            if ws_server
            else None
        )
        self.morning_brief_service = create_morning_brief_service(config)
        self.morning_brief_handler = MorningBriefHandler(
            config,
            self.morning_brief_service,
            logger=self.logger,
        )
        # 番茄钟同样要按 device_id 找活跃连接才能推画面，没有 ws_server 就不开放
        self.pomodoro_handler = (
            PomodoroHandler(config, ws_server.device_registry) if ws_server else None
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
        if self.wellbeing_service:
            async def start_wellbeing(_app):
                await self.wellbeing_service.start()

            async def stop_wellbeing(_app):
                await self.wellbeing_service.stop()

            app.on_startup.append(start_wellbeing)
            app.on_cleanup.append(stop_wellbeing)
        add_presence_routes(
            app,
            self.presence_handler,
            self.camera_stream_handler,
        )
        add_morning_brief_routes(app, self.morning_brief_handler)

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
        if self.pomodoro_handler:
            # 桌面端的番茄钟控制面板接口
            add_pomodoro_routes(app, self.pomodoro_handler)
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

                # 保持服务运行
                while True:
                    await asyncio.sleep(3600)  # 每隔 1 小时检查一次
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"HTTP服务器启动失败: {e}")
            import traceback

            self.logger.bind(tag=TAG).error(f"错误堆栈: {traceback.format_exc()}")
            raise
