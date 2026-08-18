import type {
  FrameSendResult,
  RecognitionEvent,
  RecognitionStreamOptions,
} from '../../modules/features/camera-capture/types';
import type { CameraStreamClient } from './cameraStreamClient';


const MAX_FRAME_BYTES = 1024 * 1024;
const WORKSTATION_PATTERN = /^[A-Za-z0-9._-]{1,64}$/;

export const CAMERA_STREAM_CHANNELS = {
  start: 'camera:recognition-start',
  frame: 'camera:recognition-frame',
  stop: 'camera:recognition-stop',
  event: 'camera:recognition-event',
} as const;

interface WebContentsLike {
  id: number;
  isDestroyed(): boolean;
  send(channel: string, payload: RecognitionEvent): void;
}

interface IpcEventLike {
  sender: WebContentsLike;
}

export interface CameraIpcMainLike {
  handle(
    channel: string,
    handler: (event: unknown, input?: unknown) => unknown,
  ): void;
  removeHandler(channel: string): void;
}

interface CameraClientLike {
  readonly active: boolean;
  start(options: RecognitionStreamOptions): void;
  sendFrame(jpeg: ArrayBuffer): FrameSendResult;
  stop(): void;
  subscribe(listener: (event: RecognitionEvent) => void): () => void;
}

interface RegisterCameraStreamIpcOptions {
  ipcMain: CameraIpcMainLike;
  client: Pick<
    CameraStreamClient,
    'active' | 'start' | 'sendFrame' | 'stop' | 'subscribe'
  > | CameraClientLike;
}

export interface CameraStreamIpcRegistration {
  cleanup(): void;
  isMonitoringActive(): boolean;
}

const record = (value: unknown, label: string): Record<string, unknown> => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`${label} 必须是对象`);
  }
  return value as Record<string, unknown>;
};

const parseOptions = (value: unknown): RecognitionStreamOptions => {
  const input = record(value, '识别参数');
  const allowed = new Set(['mode', 'workstationId', 'displayName']);
  const unexpected = Object.keys(input).filter((key) => !allowed.has(key));
  if (unexpected.length > 0) throw new Error(`未知识别参数：${unexpected.join(', ')}`);
  if (input.mode !== 'monitoring' && input.mode !== 'enrollment') {
    throw new Error('mode 必须是 monitoring 或 enrollment');
  }
  if (
    typeof input.workstationId !== 'string'
    || !WORKSTATION_PATTERN.test(input.workstationId)
  ) {
    throw new Error('workstationId 格式无效');
  }
  if (input.mode === 'monitoring') {
    if (input.displayName !== undefined) {
      throw new Error('monitoring 不允许 displayName');
    }
    return { mode: 'monitoring', workstationId: input.workstationId };
  }
  if (typeof input.displayName !== 'string') {
    throw new Error('displayName 是注册模式必填项');
  }
  const displayName = input.displayName.trim();
  if (!displayName || displayName.length > 64) {
    throw new Error('displayName 必须为 1 到 64 个字符');
  }
  return {
    mode: 'enrollment',
    workstationId: input.workstationId,
    displayName,
  };
};

const requireJpeg = (value: unknown): ArrayBuffer => {
  if (!(value instanceof ArrayBuffer)) throw new Error('监测帧必须是 JPEG ArrayBuffer');
  if (value.byteLength > MAX_FRAME_BYTES) throw new Error('JPEG 不能超过 1 MiB');
  const bytes = new Uint8Array(value);
  if (
    bytes.length < 4
    || bytes[0] !== 0xff
    || bytes[1] !== 0xd8
    || bytes.at(-2) !== 0xff
    || bytes.at(-1) !== 0xd9
  ) {
    throw new Error('监测帧必须是完整 JPEG');
  }
  return value;
};

const requireEvent = (value: unknown): IpcEventLike => {
  const event = record(value, 'IPC event');
  const sender = record(event.sender, 'IPC sender') as unknown as WebContentsLike;
  if (
    typeof sender.id !== 'number'
    || typeof sender.isDestroyed !== 'function'
    || typeof sender.send !== 'function'
  ) {
    throw new Error('IPC sender 无效');
  }
  return { sender };
};

export const registerCameraStreamIpc = (
  options: RegisterCameraStreamIpcOptions,
): CameraStreamIpcRegistration => {
  let owner: WebContentsLike | null = null;
  let activeMode: RecognitionStreamOptions['mode'] | null = null;
  let cleaned = false;

  const requireOwner = (eventValue: unknown): WebContentsLike => {
    const { sender } = requireEvent(eventValue);
    if (owner === null || owner.id !== sender.id) {
      throw new Error('当前窗口不是摄像头会话所有者');
    }
    return sender;
  };

  options.ipcMain.handle(CAMERA_STREAM_CHANNELS.start, async (eventValue, input) => {
    const { sender } = requireEvent(eventValue);
    if (owner !== null && owner.id !== sender.id && !owner.isDestroyed()) {
      throw new Error('摄像头正由另一个窗口使用');
    }
    const streamOptions = parseOptions(input);
    owner = sender;
    activeMode = streamOptions.mode;
    options.client.start(streamOptions);
  });
  options.ipcMain.handle(CAMERA_STREAM_CHANNELS.frame, async (eventValue, input) => {
    requireOwner(eventValue);
    return options.client.sendFrame(requireJpeg(input));
  });
  options.ipcMain.handle(CAMERA_STREAM_CHANNELS.stop, async (eventValue) => {
    const { sender } = requireEvent(eventValue);
    if (owner === null) return;
    if (owner.id !== sender.id) {
      throw new Error('当前窗口不是摄像头会话所有者');
    }
    options.client.stop();
    owner = null;
    activeMode = null;
  });

  const unsubscribe = options.client.subscribe((event) => {
    if (owner !== null && !owner.isDestroyed()) {
      owner.send(CAMERA_STREAM_CHANNELS.event, event);
    }
  });

  return {
    isMonitoringActive: () => activeMode === 'monitoring' && options.client.active,
    cleanup: () => {
      if (cleaned) return;
      cleaned = true;
      unsubscribe();
      options.client.stop();
      owner = null;
      activeMode = null;
      options.ipcMain.removeHandler(CAMERA_STREAM_CHANNELS.start);
      options.ipcMain.removeHandler(CAMERA_STREAM_CHANNELS.frame);
      options.ipcMain.removeHandler(CAMERA_STREAM_CHANNELS.stop);
    },
  };
};
