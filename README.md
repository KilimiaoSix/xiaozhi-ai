# 工伴·桌面精灵

AI Agent 驱动的打工人桌面宠物，由桌面应用、Server 和 ESP32-S3 双轴机器人组成。
产品定位与体验目标见 [AGENTS.md](AGENTS.md)。

## 通信架构

```text
Electron 桌面端 ── HTTP ──> Server ── WebSocket ──> ESP32-S3 机器人
```

- 桌面端不直接连接机器人。
- Server 与机器人只使用 WebSocket 通信。
- 当前提交先提供可在 macOS 运行的桌面端骨架，业务集成暂用 Mock 和功能占位。

## 目录

| 目录 | 角色 | 说明 |
| --- | --- | --- |
| [`desktop/`](desktop/) | 桌面端 | Electron Forge + Vite + React + TypeScript。 |
| [`server/`](server/) | **上位机** | 服务端，基于 xiaozhi-esp32-server。负责事件接入、Agent 判断、设备通信与管理后台。 |
| [`firmware/`](firmware/) | **下位机** | ESP32-S3 双轴机器人固件，基于 ESP-IDF。负责执行预设动作、表情、灯光与声音。 |

## 运行桌面端

```bash
cd desktop
npm install
npm run dev
```

### 验证与打包

```bash
cd desktop
npm test
npm run typecheck
npm run package
```

macOS arm64 应用会生成到 `desktop/out/小飞桌面机器人-darwin-arm64/`。

## 运行上位机 server

- 入口：`server/main/xiaozhi-server/app.py`
- 依赖：`server/main/xiaozhi-server/requirements.txt`
- 部署方式、管理后台与各模块说明见 [`server/README.md`](server/README.md) 与 [`server/docs/`](server/docs/)。

```bash
cd server/main/xiaozhi-server && python app.py
```

> 私有配置放在 `server/main/xiaozhi-server/data/.config.yaml`，该目录已被 `.gitignore` 排除，
> 不要把真实密钥提交进仓库。

## 构建下位机 firmware

- 芯片：`esp32s3`（`CONFIG_IDF_TARGET="esp32s3"`）
- 框架：ESP-IDF v5.5.3
- 依赖：`firmware/managed_components/` 已随仓库提交，内网无法访问乐鑫组件仓库时也可直接构建；
  版本以 `firmware/dependencies.lock` 为准。

烧录脚本见 `firmware/scripts/flash.sh`，`--help` 查看完整用法：

```bash
cd firmware && ./scripts/flash.sh /dev/cu.wchusbserial110 project
```

> `scripts/flash.sh` 里的 `IDF_DIR`、`FACTORY_BIN` 默认值是原作者本机的绝对路径，
> 其他人使用时通过同名环境变量覆盖，例如 `IDF_DIR=~/esp/esp-idf ./scripts/flash.sh ...`。

## 上游来源

| 目录 | 上游仓库 |
| --- | --- |
| `firmware/` | https://gitee.com/pengjie0668/esp32s3-ai-deskbot-kit |
| `server/` | https://github.com/xinnan-tech/xiaozhi-esp32-server |

`firmware/` 与 `server/` 以快照方式并入，未保留上游提交历史；
编译产物（`build/`、`build-macos/`）不入库。
