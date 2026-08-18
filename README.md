# 工伴·桌面精灵

AI Agent 驱动的打工人桌面宠物，由桌面应用、Server 和 ESP32-S3 双轴机器人组成。

## 通信架构

```text
Electron 桌面端 ── HTTP ──> Server ── WebSocket ──> ESP32-S3 机器人
```

- 桌面端不直接连接机器人。
- Server 与机器人只使用 WebSocket 通信。
- 当前提交先提供可在 macOS 运行的桌面端骨架，业务集成暂用 Mock 和功能占位。

## 目录

- `desktop/`：Electron Forge + Vite + React + TypeScript 桌面端。

## 运行桌面端

```bash
cd desktop
npm install
npm run dev
```

## 验证与打包

```bash
cd desktop
npm test
npm run typecheck
npm run package
```

macOS arm64 应用会生成到 `desktop/out/小飞桌面机器人-darwin-arm64/`。
