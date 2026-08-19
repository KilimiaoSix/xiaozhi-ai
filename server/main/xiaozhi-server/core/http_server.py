import asyncio
from aiohttp import web
from config.logger import setup_logging
from core.api.approval_handler import ApprovalHandler
from core.api.away_summary_handler import AwaySummaryHandler
from core.api.incident_handler import IncidentHandler
from core.api.ota_handler import OTAHandler
from core.api.owner_status_handler import OwnerStatusHandler
from core.api.vision_handler import VisionHandler
from core.api.event_handler import EventHandler
from core.api.morning_brief_handler import MorningBriefHandler
from core.api.presence_handler import PresenceHandler
from core.api.camera_stream_handler import CameraStreamHandler
from core.api.alert_relay_handler import AlertRelayHandler
from core.alert_relay.factory import create_alert_relay_service
from core.alert_relay_routes import add_alert_relay_routes
from core.approval_manager import get_approval_manager
from core.approval_routes import add_approval_routes
from datetime import date, timedelta

from core.away_ledger import (
    KIND_INCIDENT,
    SEVERITY_CRITICAL,
    SEVERITY_NORMAL,
    get_away_ledger,
)
from core.away_routes import add_away_routes
from core.camera_stream.distraction_observer import (
    DistractionObserver,
    build_reminder_text,
    create_default_detector,
)
from core.camera_stream.gesture_observer import create_gesture_observer
from core.camera_stream.observers import frame_observer_hub
from core.incident_manager import get_incident_manager
from core.incident_routes import add_incident_routes
from core.morning_brief.factory import create_morning_brief_service
from core.morning_brief_routes import add_morning_brief_routes
from core.api.pomodoro_handler import PomodoroHandler
from core.owner_status import STATUS_LEAVE, get_owner_status_store
from core.owner_status_reminder import OwnerStatusReminder
from core.owner_status_routes import add_owner_status_routes
from core.pomodoro_manager import pomodoro_manager
from plugins_func.functions.day_summary import register_summary_provider
from core.presence_arrival import create_presence_arrival_orchestrator
from core.presence_registry import PresenceRegistry
from core.presence_routes import add_presence_routes
from core.pomodoro_routes import add_pomodoro_routes
from core.timed_prompts import create_timed_prompt_scheduler
from core.visitor_flow import get_visitor_flow, make_registry_device_resolver
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
        # 装配代码可能跑在两种环境：app.py 的 async main（有事件循环，摄像头旁路
        # 观察者要靠它把工作线程里的决策跳回协程），或个别离线测试（没有循环，
        # 观察者链路整体不装）。
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        # 流程四/五：离席台账与来访应答必须与在岗编排共享同一实例，
        # 否则编排标记的离席窗口和台账记的事件不在一本账上。
        self.away_ledger = get_away_ledger(config)
        self.owner_status_store = get_owner_status_store(config)
        # 来访应答要按 device_id 找活跃连接，故与事件推送同样依赖 ws_server
        self.visitor_flow = (
            get_visitor_flow(
                config,
                owner_status_store=self.owner_status_store,
                away_ledger=self.away_ledger,
                device_resolver=make_registry_device_resolver(
                    config, ws_server.device_registry
                ),
                logger=self.logger,
            )
            if ws_server
            else None
        )
        # 在岗状态的消费方：主人到岗让机器人迎接（并汇总离席期间的事），
        # 工位持续无人让它休眠；主人不在时来了陌生人则转来访应答。
        # 需要 device_registry 才能按 device_id 找到活跃连接，故与事件推送同样依赖 ws_server；
        # 未配置 presence_robot 或走 presence_server.py 轻量入口时返回 None，接口行为不变。
        self.presence_orchestrator = create_presence_arrival_orchestrator(
            config,
            ws_server.device_registry if ws_server else None,
            logger=self.logger,
            owner_status_store=self.owner_status_store,
            away_ledger=self.away_ledger,
            visitor_flow=self.visitor_flow,
        )
        self.presence_handler = PresenceHandler(
            config,
            self.presence_registry,
            logger=self.logger,
            on_accepted=(
                self.presence_orchestrator.on_report
                if self.presence_orchestrator
                else None
            ),
        )
        self.camera_stream_handler = CameraStreamHandler(
            config,
            self.presence_registry,
            logger=self.logger,
            # 桌面摄像头流是产品默认链路，它写 registry 走的是内部直调而不是
            # /presence/report，必须单独接同一个编排回调，迎接/休眠才对它生效
            on_accepted=(
                self.presence_orchestrator.on_report
                if self.presence_orchestrator
                else None
            ),
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
        self.owner_status_handler = OwnerStatusHandler(config, self.owner_status_store)
        self.away_summary_handler = AwaySummaryHandler(
            config, self.away_ledger, logger=self.logger
        )
        # 番茄钟同样要按 device_id 找活跃连接才能推画面，没有 ws_server 就不开放
        self.pomodoro_handler = (
            PomodoroHandler(config, ws_server.device_registry) if ws_server else None
        )
        self._init_companion_orchestration(config, ws_server)

    def _primary_workstation(self) -> tuple:
        """取 presence_robot.workstations 的第一条映射 (workstation_id, device_id)。

        单人单机器人是当前产品形态；多工位部署时提醒/定时提示这类「对主人」
        的推送只发第一条映射的设备，其余工位仍有完整的来访/迎接编排。
        """
        workstations = (self.config.get("presence_robot") or {}).get("workstations")
        if isinstance(workstations, dict):
            for workstation_id, device_id in workstations.items():
                return str(workstation_id), str(device_id)
        return None, None

    def _init_companion_orchestration(self, config: dict, ws_server) -> None:
        """小飞全功能串联的装配点：审批手势、告警、分心检测、状态提醒、定时提示。

        各模块自身都做了「依赖缺失就降级」：没有 ws_server 不播报、没有事件循环
        不装摄像头旁路观察者、模型文件缺失观察者置 disabled。装配失败不该让
        HTTP 服务起不来。
        """
        registry = ws_server.device_registry if ws_server else None

        # ── 手势审批（流程三）────────────────────────────
        # 必须走单例：手势观察者与 HTTP 接口要操作同一批请求
        self.approval_manager = get_approval_manager(
            config, registry, logger=self.logger
        )
        self.approval_handler = ApprovalHandler(
            config, registry, manager=self.approval_manager
        )
        self.gesture_observer = (
            create_gesture_observer(
                config, self.approval_manager, loop=self._loop, logger=self.logger
            )
            if self._loop
            else None
        )
        if self.gesture_observer is not None:
            frame_observer_hub.register("gesture_approval", self.gesture_observer)

        # ── 线上告警（流程七）────────────────────────────
        self.incident_handler = (
            IncidentHandler(config, registry) if ws_server else None
        )
        if self.incident_handler:
            # P2/P3 不播报，进离席台账等返岗/日终汇总；P0/P1 由 manager 直接播报
            get_incident_manager(config).set_on_low_severity(
                lambda incident: get_away_ledger(config).record(
                    KIND_INCIDENT,
                    f"{incident['service']} {incident['title']}",
                    task_key=f"incident:{incident['incident_id']}",
                    severity=SEVERITY_NORMAL,
                    source="incident",
                )
            )
            # P0/P1 播报后也要记台账：主人不在时那一嗓子是对着空工位喊的，
            # 没有这条，返岗汇总的「严重告警优先」桶在真实告警链路里永远空转
            get_incident_manager(config).set_on_announced(
                lambda incident: get_away_ledger(config).record(
                    KIND_INCIDENT,
                    f"{incident['service']} {incident['title']}",
                    task_key=f"incident:{incident['incident_id']}",
                    severity=SEVERITY_CRITICAL,
                    source="incident",
                )
            )

        # ── 日终总结数据源（流程八）──────────────────────
        # day_summary 语音函数按注册表取数；不接这三个槽位，
        # 「总结一下今天」永远只会说兜底话术
        register_summary_provider(
            "agent_events", lambda: get_away_ledger(config).events_today()
        )
        register_summary_provider(
            "incidents", lambda: get_incident_manager(config).list_today()
        )

        def _tomorrow_status() -> dict:
            record = get_owner_status_store(config).get()
            if record.get("state") != STATUS_LEAVE:
                return {"state": "available"}
            tomorrow = date.today() + timedelta(days=1)
            try:
                start = date.fromisoformat(record.get("leave_start") or "")
                end = date.fromisoformat(record.get("leave_end") or "")
            except ValueError:
                return {"state": "available"}
            if start <= tomorrow <= end:
                return {"state": "leave"}
            return {"state": "available"}

        register_summary_provider("tomorrow_status", _tomorrow_status)

        # ── 分心检测（流程六）────────────────────────────
        self.distraction_observer = None
        distraction_section = (config or {}).get("distraction") or {}
        if (
            ws_server
            and self._loop
            and isinstance(distraction_section, dict)
            and distraction_section.get("enabled", False)
        ):
            workstation_devices = dict(
                (config.get("presence_robot") or {}).get("workstations") or {}
            )
            loop = self._loop

            def _distraction_active(workstation_id: str) -> bool:
                device_id = workstation_devices.get(workstation_id)
                # 只在番茄钟专注相位进行中检测——需求硬性要求，不做默认监控
                return bool(device_id) and pomodoro_manager.is_focus_active(device_id)

            async def _push_distraction_reminder(device_id: str) -> None:
                from core.handle.pushHandle import push_work_event

                conn = registry.get(device_id)
                if conn is None:
                    return
                result = await pomodoro_manager.status(device_id)
                snapshot = result.get("status") or {}
                remaining = (
                    snapshot.get("remaining_s") if snapshot.get("active") else None
                )
                await push_work_event(
                    conn,
                    text=build_reminder_text(remaining),
                    emotion="thinking",
                    status="专注提醒",
                    speak=True,
                )

            def _on_distraction(workstation_id: str) -> None:
                device_id = workstation_devices.get(workstation_id)
                if not device_id:
                    return
                # on_frame 跑在推理工作线程，推送必须跳回事件循环
                asyncio.run_coroutine_threadsafe(
                    _push_distraction_reminder(device_id), loop
                )

            self.distraction_observer = DistractionObserver(
                active_lookup=_distraction_active,
                on_distraction=_on_distraction,
                detector_factory=lambda: create_default_detector(
                    distraction_section.get(
                        "model_path", "data/models/efficientdet_lite0.tflite"
                    )
                ),
                trigger_seconds=float(distraction_section.get("trigger_seconds", 8.0)),
                min_score=float(distraction_section.get("min_score", 0.4)),
                logger=self.logger,
            )
            frame_observer_hub.register("distraction", self.distraction_observer)

        # ── 状态过期提醒（流程四·状态过期）+ 定时提示（需求分析第 5 条）──
        primary_workstation, primary_device = self._primary_workstation()

        def _presence_effective_state():
            if not primary_workstation:
                return None
            record = self.presence_registry.get(primary_workstation)
            return (record or {}).get("effective_state")

        self.owner_status_reminder = None
        if ws_server and primary_device:

            def _resolve_owner_device():
                conn = registry.get(primary_device)
                return (primary_device, conn) if conn else None

            self.owner_status_reminder = OwnerStatusReminder(
                self.owner_status_store,
                _resolve_owner_device,
                presence_lookup=_presence_effective_state,
                logger=self.logger,
            )

        self.timed_prompt_scheduler = (
            create_timed_prompt_scheduler(
                config,
                device_resolver=registry.get,
                presence_lookup=lambda ws_id: (
                    (self.presence_registry.get(ws_id) or {}).get("effective_state")
                ),
                logger=self.logger,
            )
            if ws_server
            else None
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
        add_owner_status_routes(app, self.owner_status_handler)
        add_away_routes(app, self.away_summary_handler)
        add_approval_routes(app, self.approval_handler)
        if self.incident_handler:
            add_incident_routes(app, self.incident_handler)
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

                # 周期型编排任务与 HTTP 服务同生共死：
                # 状态过期提醒（流程四）与定时提示（需求分析第 5 条）
                if self.owner_status_reminder is not None:
                    self.owner_status_reminder.start()
                if self.timed_prompt_scheduler is not None:
                    self.timed_prompt_scheduler.start()

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
