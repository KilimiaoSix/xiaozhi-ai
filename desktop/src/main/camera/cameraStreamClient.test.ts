import { Buffer } from 'node:buffer';
import { describe, expect, it, vi } from 'vitest';

import {
  CameraStreamClient,
  type CameraSocketLike,
  type CameraWebSocketFactory,
} from './cameraStreamClient';


class FakeSocket implements CameraSocketLike {
  readyState = 0;
  bufferedAmount = 0;
  sent: unknown[] = [];
  closed = false;
  private listeners = new Map<string, Array<(...args: unknown[]) => void>>();

  on(event: string, listener: (...args: unknown[]) => void): this {
    const current = this.listeners.get(event) ?? [];
    current.push(listener);
    this.listeners.set(event, current);
    return this;
  }

  send(data: unknown): void {
    this.sent.push(data);
  }

  close(): void {
    this.closed = true;
    this.readyState = 3;
  }

  emit(event: string, ...args: unknown[]): void {
    for (const listener of this.listeners.get(event) ?? []) listener(...args);
  }

  open(): void {
    this.readyState = 1;
    this.emit('open');
  }

  serverEvent(payload: unknown): void {
    this.emit('message', Buffer.from(JSON.stringify(payload)));
  }

  serverClose(): void {
    this.readyState = 3;
    this.emit('close');
  }
}


const makeHarness = (serverUrl = 'http://127.0.0.1:8003') => {
  const sockets: FakeSocket[] = [];
  const connections: Array<{ url: string; headers: Record<string, string> }> = [];
  const factory: CameraWebSocketFactory = (url, options) => {
    const socket = new FakeSocket();
    sockets.push(socket);
    connections.push({ url, headers: options.headers });
    return socket;
  };
  const timers: Array<{
    callback: () => void;
    delay: number;
    cancelled: boolean;
  }> = [];
  const uuids = Array.from({ length: 10 }, (_, index) => `session-${index + 1}`);
  const client = new CameraStreamClient({
    serverUrl,
    authToken: 'top-secret',
    webSocketFactory: factory,
    randomUUID: () => uuids.shift() ?? 'session-overflow',
    setTimer: (callback, delay) => {
      const timer = { callback, delay, cancelled: false };
      timers.push(timer);
      return timer;
    },
    clearTimer: (timer) => {
      (timer as typeof timers[number]).cancelled = true;
    },
  });
  return { client, sockets, connections, timers };
};


describe('CameraStreamClient', () => {
  it('converts the configured URL and sends auth only in the handshake header', () => {
    const { client, sockets, connections } = makeHarness(
      'https://server.example:9443/base?token=wrong',
    );

    client.start({ mode: 'monitoring', workstationId: 'desk-local' });
    sockets[0].open();

    expect(connections).toEqual([{
      url: 'wss://server.example:9443/xiaozhi/presence/stream',
      headers: { Authorization: 'Bearer top-secret' },
    }]);
    const start = JSON.parse(String(sockets[0].sent[0]));
    expect(start).toEqual({
      type: 'start',
      schema_version: '1.0',
      mode: 'monitoring',
      session_id: 'session-1',
      workstation_id: 'desk-local',
    });
    expect(JSON.stringify(start)).not.toContain('top-secret');
  });

  it('sends binary frames only after ready and drops when buffered data exceeds 1 MiB', () => {
    const { client, sockets } = makeHarness();
    const jpeg = new Uint8Array([0xff, 0xd8, 0xff, 0xd9]).buffer;

    client.start({ mode: 'monitoring', workstationId: 'desk-local' });
    sockets[0].open();
    expect(client.sendFrame(jpeg)).toBe('not-ready');

    sockets[0].serverEvent({
      type: 'ready', session_id: 'session-1', sequence: 0,
    });
    expect(client.sendFrame(jpeg)).toBe('sent');
    expect(Buffer.isBuffer(sockets[0].sent[1])).toBe(true);

    sockets[0].bufferedAmount = 1024 * 1024 + 1;
    expect(client.sendFrame(jpeg)).toBe('dropped');
    expect(sockets[0].sent).toHaveLength(2);
  });

  it('forwards recognized server events without exposing transport credentials', () => {
    const { client, sockets } = makeHarness();
    const events: unknown[] = [];
    client.subscribe((event) => events.push(event));
    client.start({ mode: 'monitoring', workstationId: 'desk-local' });
    sockets[0].open();
    sockets[0].serverEvent({
      type: 'ready', session_id: 'session-1', sequence: 0,
    });

    sockets[0].serverEvent({
      type: 'recognition_result',
      session_id: 'session-1',
      sequence: 4,
      presence: { state: 'present', changed: true },
      identity: { state: 'owner', face_count: 1, similarity: 0.73 },
      metrics: { processed_frames: 4 },
    });

    expect(events.at(-1)).toMatchObject({
      type: 'recognition_result',
      sequence: 4,
      identity: { state: 'owner', similarity: 0.73 },
    });
    expect(JSON.stringify(events)).not.toContain('top-secret');
  });

  it('reconnects forever with capped backoff and a fresh session id', () => {
    const { client, sockets, timers } = makeHarness();
    client.start({ mode: 'monitoring', workstationId: 'desk-local' });
    sockets[0].open();

    const delays: number[] = [];
    for (let attempt = 0; attempt < 8; attempt += 1) {
      sockets[attempt].serverClose();
      const timer = timers.at(-1)!;
      delays.push(timer.delay);
      timer.callback();
      sockets[attempt + 1].open();
    }

    expect(delays).toEqual([1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000]);
    const sessionIds = sockets.map((socket) => JSON.parse(String(socket.sent[0])).session_id);
    expect(sessionIds).toEqual([
      'session-1', 'session-2', 'session-3', 'session-4', 'session-5',
      'session-6', 'session-7', 'session-8', 'session-9',
    ]);
    expect(client.active).toBe(true);
  });

  it('stop cancels reconnect and closes the current socket', () => {
    const { client, sockets, timers } = makeHarness();
    client.start({ mode: 'monitoring', workstationId: 'desk-local' });
    sockets[0].open();
    sockets[0].serverClose();
    const pending = timers[0];

    client.stop();

    expect(pending.cancelled).toBe(true);
    expect(client.active).toBe(false);
    pending.callback();
    expect(sockets).toHaveLength(1);
  });

  it('includes a trimmed display name for enrollment', () => {
    const { client, sockets } = makeHarness();

    client.start({
      mode: 'enrollment', workstationId: 'desk-local', displayName: ' 主人 ',
    });
    sockets[0].open();

    expect(JSON.parse(String(sockets[0].sent[0]))).toMatchObject({
      mode: 'enrollment',
      display_name: '主人',
    });
  });
});
