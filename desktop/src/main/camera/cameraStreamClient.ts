import { Buffer } from 'node:buffer';
import { randomUUID as nodeRandomUUID } from 'node:crypto';

import WebSocket from 'ws';

import type {
  FrameSendResult,
  RecognitionEvent,
  RecognitionStreamOptions,
} from '../../modules/features/camera-capture/types';


const MAX_BUFFERED_BYTES = 1024 * 1024;
const RECONNECT_DELAYS_MS = [1000, 2000, 4000, 8000, 16000, 30000] as const;

export interface CameraSocketLike {
  readyState: number;
  bufferedAmount: number;
  on(event: string, listener: (...args: unknown[]) => void): this;
  send(data: unknown): void;
  close(): void;
}

export interface CameraWebSocketFactoryOptions {
  headers: Record<string, string>;
}

export type CameraWebSocketFactory = (
  url: string,
  options: CameraWebSocketFactoryOptions,
) => CameraSocketLike;

type TimerHandle = unknown;

interface CameraStreamClientOptions {
  serverUrl?: string;
  authToken?: string;
  webSocketFactory?: CameraWebSocketFactory;
  randomUUID?: () => string;
  setTimer?: (callback: () => void, delay: number) => TimerHandle;
  clearTimer?: (timer: TimerHandle) => void;
}

type EventListener = (event: RecognitionEvent) => void;

const streamUrl = (serverUrl: string): string => {
  const url = new URL(serverUrl);
  if (url.protocol === 'http:') url.protocol = 'ws:';
  else if (url.protocol === 'https:') url.protocol = 'wss:';
  else throw new Error('Server URL must use http or https');
  url.pathname = '/xiaozhi/presence/stream';
  url.search = '';
  url.hash = '';
  return url.toString();
};

const decodeMessage = (value: unknown): RecognitionEvent | null => {
  let text: string;
  if (typeof value === 'string') text = value;
  else if (Buffer.isBuffer(value)) text = value.toString('utf8');
  else if (value instanceof ArrayBuffer) text = Buffer.from(value).toString('utf8');
  else if (ArrayBuffer.isView(value)) {
    text = Buffer.from(value.buffer, value.byteOffset, value.byteLength).toString('utf8');
  } else return null;

  try {
    const payload: unknown = JSON.parse(text);
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null;
    const event = payload as RecognitionEvent;
    return typeof event.type === 'string' ? event : null;
  } catch {
    return null;
  }
};

export class CameraStreamClient {
  private readonly serverUrl: string;
  private readonly authToken: string;
  private readonly webSocketFactory: CameraWebSocketFactory;
  private readonly randomUUID: () => string;
  private readonly setTimer: (callback: () => void, delay: number) => TimerHandle;
  private readonly clearTimer: (timer: TimerHandle) => void;
  private readonly listeners = new Set<EventListener>();
  private socket: CameraSocketLike | null = null;
  private reconnectTimer: TimerHandle | null = null;
  private desiredOptions: RecognitionStreamOptions | null = null;
  private sessionId = '';
  private ready = false;
  private reconnectAttempt = 0;

  constructor(options: CameraStreamClientOptions = {}) {
    this.serverUrl = options.serverUrl
      ?? process.env.XIAOFEI_SERVER_URL
      ?? 'http://127.0.0.1:8003';
    this.authToken = options.authToken
      ?? process.env.XIAOFEI_SERVER_AUTH_TOKEN
      ?? '';
    this.webSocketFactory = options.webSocketFactory ?? ((url, init) => (
      new WebSocket(url, { headers: init.headers }) as unknown as CameraSocketLike
    ));
    this.randomUUID = options.randomUUID ?? nodeRandomUUID;
    this.setTimer = options.setTimer ?? ((callback, delay) => setTimeout(callback, delay));
    this.clearTimer = options.clearTimer ?? ((timer) => clearTimeout(
      timer as ReturnType<typeof setTimeout>,
    ));
  }

  get active(): boolean {
    return this.desiredOptions !== null;
  }

  start(options: RecognitionStreamOptions): void {
    this.stop();
    this.desiredOptions = { ...options };
    this.reconnectAttempt = 0;
    this.connect();
  }

  sendFrame(jpeg: ArrayBuffer): FrameSendResult {
    const socket = this.socket;
    if (!this.ready || socket?.readyState !== WebSocket.OPEN) return 'not-ready';
    if (socket.bufferedAmount > MAX_BUFFERED_BYTES) return 'dropped';
    socket.send(Buffer.from(jpeg));
    return 'sent';
  }

  stop(): void {
    this.desiredOptions = null;
    this.ready = false;
    if (this.reconnectTimer !== null) {
      this.clearTimer(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    const socket = this.socket;
    this.socket = null;
    if (socket !== null && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: 'stop' }));
    }
    socket?.close();
    this.emit({ type: 'connection', state: 'idle' });
  }

  subscribe(listener: EventListener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  private connect(): void {
    const desired = this.desiredOptions;
    if (desired === null) return;
    this.sessionId = this.randomUUID();
    this.ready = false;
    const headers: Record<string, string> = {};
    if (this.authToken) headers.Authorization = `Bearer ${this.authToken}`;
    const socket = this.webSocketFactory(streamUrl(this.serverUrl), { headers });
    this.socket = socket;
    this.emit({
      type: 'connection',
      state: this.reconnectAttempt === 0 ? 'connecting' : 'reconnecting',
    });

    socket.on('open', () => {
      if (socket !== this.socket || this.desiredOptions === null) return;
      const start: Record<string, unknown> = {
        type: 'start',
        schema_version: '1.0',
        mode: desired.mode,
        session_id: this.sessionId,
        workstation_id: desired.workstationId,
      };
      if (desired.mode === 'enrollment') {
        start.display_name = desired.displayName?.trim() ?? '';
      }
      socket.send(JSON.stringify(start));
    });
    socket.on('message', (...args) => {
      if (socket !== this.socket) return;
      const event = decodeMessage(args[0]);
      if (event === null || event.session_id !== this.sessionId) return;
      if (event.type === 'ready') {
        this.ready = true;
        this.reconnectAttempt = 0;
        this.emit({ type: 'connection', state: 'online' });
      }
      this.emit(event);
    });
    socket.on('error', () => {
      if (socket === this.socket) this.ready = false;
    });
    socket.on('close', () => {
      if (socket !== this.socket) return;
      this.socket = null;
      this.ready = false;
      this.scheduleReconnect();
    });
  }

  private scheduleReconnect(): void {
    if (this.desiredOptions === null || this.reconnectTimer !== null) return;
    const delay = RECONNECT_DELAYS_MS[
      Math.min(this.reconnectAttempt, RECONNECT_DELAYS_MS.length - 1)
    ];
    this.reconnectAttempt += 1;
    this.emit({ type: 'connection', state: 'reconnecting', retryInMs: delay });
    const timer = this.setTimer(() => {
      if (this.reconnectTimer !== timer) return;
      this.reconnectTimer = null;
      this.connect();
    }, delay);
    this.reconnectTimer = timer;
  }

  private emit(event: RecognitionEvent): void {
    for (const listener of this.listeners) listener(event);
  }
}
