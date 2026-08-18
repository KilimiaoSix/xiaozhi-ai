import { describe, expect, it, vi } from 'vitest';

import {
  CAMERA_STREAM_CHANNELS,
  registerCameraStreamIpc,
  type CameraIpcMainLike,
} from './cameraStreamIpc';


const jpeg = new Uint8Array([0xff, 0xd8, 0xff, 0xd9]).buffer;

const makeHarness = () => {
  const handlers = new Map<string, (event: unknown, input?: unknown) => unknown>();
  const removed: string[] = [];
  const ipcMain: CameraIpcMainLike = {
    handle: (channel, handler) => handlers.set(channel, handler),
    removeHandler: (channel) => removed.push(channel),
  };
  let eventListener: ((event: never) => void) | undefined;
  const client = {
    active: false,
    start: vi.fn(function start() { client.active = true; }),
    sendFrame: vi.fn(() => 'sent' as const),
    stop: vi.fn(function stop() { client.active = false; }),
    subscribe: vi.fn((listener: (event: never) => void) => {
      eventListener = listener;
      return vi.fn();
    }),
  };
  const registration = registerCameraStreamIpc({
    ipcMain,
    client,
  });
  const owner = {
    id: 10,
    isDestroyed: vi.fn(() => false),
    send: vi.fn(),
  };
  const other = {
    id: 11,
    isDestroyed: vi.fn(() => false),
    send: vi.fn(),
  };
  const event = (sender = owner) => ({ sender });
  return {
    handlers, removed, client, registration, owner, other, event, eventListener: () => eventListener!,
  };
};


describe('registerCameraStreamIpc', () => {
  it('validates start options and keeps one client owned by the starting renderer', async () => {
    const { handlers, client, owner, other, event, registration } = makeHarness();
    const start = handlers.get(CAMERA_STREAM_CHANNELS.start)!;

    await expect(start(event(), {
      mode: 'monitoring', workstationId: 'desk-local',
    })).resolves.toBeUndefined();
    expect(client.start).toHaveBeenCalledWith({
      mode: 'monitoring', workstationId: 'desk-local',
    });
    expect(registration.isMonitoringActive()).toBe(true);

    await expect(start(event(other), {
      mode: 'monitoring', workstationId: 'desk-other',
    })).rejects.toThrow('另一个窗口');
    expect(client.start).toHaveBeenCalledTimes(1);

    await expect(start(event(), {
      mode: 'enrollment', workstationId: 'bad id', displayName: '主人',
    })).rejects.toThrow('workstationId');
    await expect(start(event(), {
      mode: 'enrollment', workstationId: 'desk-local', displayName: '   ',
    })).rejects.toThrow('displayName');
    expect(owner.id).toBe(10);
  });

  it('accepts only bounded complete JPEG ArrayBuffers from the owner', async () => {
    const { handlers, client, other, event } = makeHarness();
    await handlers.get(CAMERA_STREAM_CHANNELS.start)!(event(), {
      mode: 'monitoring', workstationId: 'desk-local',
    });
    const frame = handlers.get(CAMERA_STREAM_CHANNELS.frame)!;

    await expect(frame(event(), jpeg)).resolves.toBe('sent');
    expect(client.sendFrame).toHaveBeenCalledWith(jpeg);
    await expect(frame(event(other), jpeg)).rejects.toThrow('会话所有者');
    await expect(frame(event(), new ArrayBuffer(0))).rejects.toThrow('JPEG');
    await expect(frame(event(), new Uint8Array([1, 2, 3]).buffer)).rejects.toThrow('JPEG');
    await expect(
      frame(event(), new ArrayBuffer(1024 * 1024 + 1)),
    ).rejects.toThrow('1 MiB');
  });

  it('forwards events only to the live owner and stop clears active monitoring', async () => {
    const { handlers, client, owner, other, event, eventListener, registration } = makeHarness();
    await handlers.get(CAMERA_STREAM_CHANNELS.start)!(event(), {
      mode: 'monitoring', workstationId: 'desk-local',
    });

    eventListener()({ type: 'ready', sequence: 0 } as never);
    expect(owner.send).toHaveBeenCalledWith(
      CAMERA_STREAM_CHANNELS.event,
      { type: 'ready', sequence: 0 },
    );
    expect(other.send).not.toHaveBeenCalled();

    owner.isDestroyed.mockReturnValue(true);
    eventListener()({ type: 'recognition_result', sequence: 1 } as never);
    expect(owner.send).toHaveBeenCalledTimes(1);

    await expect(
      handlers.get(CAMERA_STREAM_CHANNELS.stop)!(event(other)),
    ).rejects.toThrow('会话所有者');
    owner.isDestroyed.mockReturnValue(false);
    await handlers.get(CAMERA_STREAM_CHANNELS.stop)!(event());
    expect(client.stop).toHaveBeenCalledTimes(1);
    expect(registration.isMonitoringActive()).toBe(false);
  });

  it('treats stop as idempotent when no camera session is active', async () => {
    const { handlers, client, event } = makeHarness();
    const stop = handlers.get(CAMERA_STREAM_CHANNELS.stop)!;

    await expect(stop(event())).resolves.toBeUndefined();
    await handlers.get(CAMERA_STREAM_CHANNELS.start)!(event(), {
      mode: 'monitoring', workstationId: 'desk-local',
    });
    await expect(stop(event())).resolves.toBeUndefined();
    await expect(stop(event())).resolves.toBeUndefined();

    expect(client.stop).toHaveBeenCalledOnce();
  });

  it('cleanup stops the client, unsubscribes, and removes every handler', async () => {
    const { handlers, removed, client, event, registration } = makeHarness();
    await handlers.get(CAMERA_STREAM_CHANNELS.start)!(event(), {
      mode: 'enrollment', workstationId: 'desk-local', displayName: ' 主人 ',
    });
    const unsubscribe = client.subscribe.mock.results[0].value as ReturnType<typeof vi.fn>;

    registration.cleanup();

    expect(client.stop).toHaveBeenCalledTimes(1);
    expect(unsubscribe).toHaveBeenCalledTimes(1);
    expect(removed.sort()).toEqual([
      CAMERA_STREAM_CHANNELS.frame,
      CAMERA_STREAM_CHANNELS.start,
      CAMERA_STREAM_CHANNELS.stop,
    ].sort());
  });
});
