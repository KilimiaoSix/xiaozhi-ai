import {
  createContext,
  type PropsWithChildren,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';

import { VideoFrameProducer } from '../services/videoFrameProducer';
import type {
  EnrollmentState,
  FaceRecognitionState,
  PresenceRecognitionState,
  RecognitionConnectionState,
  RecognitionEvent,
  RecognitionMetrics,
} from '../types';

const WORKSTATION_ID = 'desktop-local';

// 监测开关承载的是「持续意图」：切页、最小化、Server 断线都不放弃监测,
// 唯独应用重启会把它归零——服务端或应用任何一次重启都让摄像头链路静默断掉,
// 迎接/手势审批/分心检测三个功能一起哑掉,而用户以为它还开着。
// 意图落 localStorage,挂载时自动恢复;首次使用无记录,保持默认关闭。
const MONITORING_INTENT_KEY = 'xiaofei.camera.monitoring-intent';

const readMonitoringIntent = (): boolean => {
  try {
    return window.localStorage.getItem(MONITORING_INTENT_KEY) === 'on';
  } catch {
    return false;
  }
};

const writeMonitoringIntent = (on: boolean): void => {
  try {
    window.localStorage.setItem(MONITORING_INTENT_KEY, on ? 'on' : 'off');
  } catch {
    // localStorage 不可用只影响下次启动的恢复,不影响本次会话
  }
};

const initialPresence: PresenceRecognitionState = {
  state: 'starting',
  changed: false,
};

const initialIdentity: FaceRecognitionState = {
  state: 'starting',
  faceCount: 0,
  faceDetected: false,
  matched: false,
};

const initialMetrics: RecognitionMetrics = {
  sentFrames: 0,
  clientDropped: 0,
  processedFrames: 0,
  serverDropped: 0,
  lastResultAt: '',
};

const initialEnrollment: EnrollmentState = {
  status: 'idle',
  accepted: 0,
  required: 20,
  reason: '',
  sampleId: '',
  sampleCount: 0,
  storedAt: '',
};

export interface CameraMonitoringValue {
  enabled: boolean;
  stream: MediaStream | null;
  devices: MediaDeviceInfo[];
  selectedDeviceId: string;
  connection: RecognitionConnectionState;
  presence: PresenceRecognitionState;
  identity: FaceRecognitionState;
  metrics: RecognitionMetrics;
  enrollment: EnrollmentState;
  errorMessage: string;
  startMonitoring(): Promise<void>;
  stopMonitoring(): Promise<void>;
  startEnrollment(displayName: string): Promise<void>;
  cancelEnrollment(): Promise<void>;
  selectDevice(deviceId: string): Promise<void>;
}

const CameraMonitoringContext = createContext<CameraMonitoringValue | null>(null);

const numberValue = (value: unknown, fallback = 0): number => (
  typeof value === 'number' && Number.isFinite(value) ? value : fallback
);

const recordValue = (value: unknown): Record<string, unknown> => (
  value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
);

const cameraErrorMessage = (error: unknown): string => {
  if (error instanceof DOMException) {
    if (error.name === 'NotAllowedError') return '摄像头权限未开启';
    if (error.name === 'NotFoundError') return '没有检测到可用摄像头';
    if (error.name === 'NotReadableError') return '摄像头正被其他应用占用';
    if (error.name === 'OverconstrainedError') return '选择的摄像头当前不可用';
  }
  return error instanceof Error ? error.message : '摄像头启动失败';
};

export function CameraMonitoringProvider({ children }: PropsWithChildren) {
  const [enabled, setEnabled] = useState(false);
  const [stream, setStream] = useState<MediaStream | null>(null);
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [selectedDeviceId, setSelectedDeviceId] = useState('');
  const [connection, setConnection] = useState<RecognitionConnectionState>('idle');
  const [presence, setPresence] = useState(initialPresence);
  const [identity, setIdentity] = useState(initialIdentity);
  const [metrics, setMetrics] = useState(initialMetrics);
  const [enrollment, setEnrollment] = useState(initialEnrollment);
  const [errorMessage, setErrorMessage] = useState('');
  const hiddenVideoRef = useRef<HTMLVideoElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const selectedDeviceRef = useRef('');
  const desiredModeRef = useRef<'monitoring' | 'enrollment' | null>(null);
  const producerRef = useRef<VideoFrameProducer | null>(null);
  const cameraRetryRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const stopCamera = useCallback(() => {
    if (cameraRetryRef.current !== null) {
      clearTimeout(cameraRetryRef.current);
      cameraRetryRef.current = null;
    }
    producerRef.current?.stop();
    producerRef.current = null;
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    setStream(null);
  }, []);

  const startCamera = useCallback(async (deviceId?: string): Promise<MediaStream> => {
    producerRef.current?.stop();
    producerRef.current = null;
    streamRef.current?.getTracks().forEach((track) => track.stop());
    const nextStream = await navigator.mediaDevices.getUserMedia({
      audio: false,
      video: {
        deviceId: deviceId ? { exact: deviceId } : undefined,
        width: { ideal: 1280 },
        height: { ideal: 720 },
        facingMode: 'user',
      },
    });
    streamRef.current = nextStream;
    setStream(nextStream);
    const nextDevices = (await navigator.mediaDevices.enumerateDevices())
      .filter((device) => device.kind === 'videoinput');
    setDevices(nextDevices);
    const activeDeviceId = nextStream.getVideoTracks()[0]?.getSettings().deviceId
      ?? deviceId
      ?? nextDevices[0]?.deviceId
      ?? '';
    selectedDeviceRef.current = activeDeviceId;
    setSelectedDeviceId(activeDeviceId);
    return nextStream;
  }, []);

  const scheduleCameraRecovery = useCallback(() => {
    if (desiredModeRef.current === null || cameraRetryRef.current !== null) return;
    cameraRetryRef.current = setTimeout(() => {
      cameraRetryRef.current = null;
      if (desiredModeRef.current === null) return;
      void startCamera(selectedDeviceRef.current || undefined)
        .then(() => setErrorMessage(''))
        .catch((error) => {
          setErrorMessage(cameraErrorMessage(error));
          scheduleCameraRecovery();
        });
    }, 2000);
  }, [startCamera]);

  useEffect(() => {
    const video = hiddenVideoRef.current;
    if (!video) return;
    video.srcObject = stream;
    if (stream) {
      // StrictMode 与换流会先卸载再重挂，挂起的 play() 被下面的 srcObject 置空
      // 打断后抛 AbortError——属正常时序，接住以免开发日志刷未处理 rejection、
      // 掩盖真实错误。真正的播放失败留一条 warn，摄像头恢复由既有重试路径兜底。
      // jsdom 的 play() 返回 undefined，必须用可选链。
      void video.play()?.catch((error: unknown) => {
        if ((error as DOMException | null)?.name !== 'AbortError') {
          console.warn('隐藏视频启动播放失败', error);
        }
      });
    }
    return () => { video.srcObject = null; };
  }, [stream]);

  useEffect(() => {
    const video = hiddenVideoRef.current;
    if (!stream || !video || desiredModeRef.current === null) return;
    const producer = new VideoFrameProducer({
      video,
      // 轨道直采:窗口最小化/被遮挡时 <video> 解码被挂起,元素路径会静默断流
      track: stream.getVideoTracks()[0],
      sendFrame: (jpeg) => window.xiaofei.camera.recognition.sendFrame(jpeg),
      onSnapshot: (snapshot) => setMetrics((current) => ({
        ...current,
        sentFrames: snapshot.sent,
        clientDropped: snapshot.dropped,
      })),
    });
    producerRef.current = producer;
    producer.start();
    return () => {
      producer.stop();
      if (producerRef.current === producer) producerRef.current = null;
    };
  }, [stream]);

  useEffect(() => {
    const track = stream?.getVideoTracks()[0];
    if (!track) return;
    const recover = () => {
      if (desiredModeRef.current === null) return;
      setErrorMessage('摄像头连接中断，正在恢复');
      void startCamera(selectedDeviceRef.current).catch((error) => {
        setErrorMessage(cameraErrorMessage(error));
        scheduleCameraRecovery();
      });
    };
    track.addEventListener('ended', recover);
    return () => track.removeEventListener('ended', recover);
  }, [scheduleCameraRecovery, startCamera, stream]);

  const stopSession = useCallback(async () => {
    desiredModeRef.current = null;
    producerRef.current?.stop();
    try {
      await window.xiaofei.camera.recognition.stop();
    } catch {
      // The local camera must still be released if the main process is exiting.
    } finally {
      stopCamera();
      setConnection('idle');
    }
  }, [stopCamera]);

  useEffect(() => {
    const unsubscribe = window.xiaofei.camera.recognition.onEvent(
      (event: RecognitionEvent) => {
        if (event.type === 'connection') {
          const state = event.state;
          if (
            state === 'idle'
            || state === 'connecting'
            || state === 'online'
            || state === 'reconnecting'
          ) setConnection(state);
          return;
        }
        if (event.type === 'error') {
          setErrorMessage(
            typeof event.message === 'string' ? event.message : '识别服务暂时不可用',
          );
          if (desiredModeRef.current === 'enrollment') {
            setEnrollment((current) => ({ ...current, status: 'error' }));
            void stopSession();
          }
          return;
        }
        if (event.type === 'recognition_result') {
          const nextPresence = recordValue(event.presence);
          const nextIdentity = recordValue(event.identity);
          const nextMetrics = recordValue(event.metrics);
          const faceCount = numberValue(nextIdentity.face_count);
          setPresence({
            state: nextPresence.state as PresenceRecognitionState['state'],
            changed: nextPresence.changed === true,
          });
          setIdentity({
            state: nextIdentity.state as FaceRecognitionState['state'],
            faceCount,
            faceDetected: faceCount > 0,
            matched: nextIdentity.matched === true,
            ...(typeof nextIdentity.similarity === 'number'
              ? { similarity: nextIdentity.similarity }
              : {}),
            ...(typeof nextIdentity.threshold === 'number'
              ? { threshold: nextIdentity.threshold }
              : {}),
          });
          setMetrics((current) => ({
            ...current,
            processedFrames: numberValue(nextMetrics.processed_frames),
            serverDropped: numberValue(nextMetrics.server_dropped),
            lastResultAt: typeof event.processed_at === 'string' ? event.processed_at : '',
          }));
          setErrorMessage('');
          return;
        }
        if (event.type === 'enrollment_progress') {
          setEnrollment((current) => ({
            ...current,
            status: 'running',
            accepted: numberValue(event.accepted),
            required: numberValue(event.required, 20),
            reason: typeof event.reason === 'string' ? event.reason : '',
          }));
          return;
        }
        if (event.type === 'enrollment_complete') {
          desiredModeRef.current = null;
          setEnrollment((current) => ({
            ...current,
            status: 'success',
            accepted: current.required,
            reason: 'complete',
            sampleId: typeof event.sample_id === 'string' ? event.sample_id : '',
            sampleCount: numberValue(event.sample_count),
            storedAt: typeof event.stored_at === 'string' ? event.stored_at : '',
          }));
          producerRef.current?.stop();
          void window.xiaofei.camera.recognition.stop();
          stopCamera();
          setConnection('idle');
          setErrorMessage('');
        }
      },
    );
    return () => unsubscribe();
  }, [stopCamera, stopSession]);

  useEffect(() => () => {
    desiredModeRef.current = null;
    producerRef.current?.stop();
    void window.xiaofei.camera.recognition.stop();
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
  }, []);

  // 启动自动恢复:上次退出前监测开着,本次挂载后直接续上。
  // startMonitoring 内部自带错误处理(设 errorMessage + 定时重试),
  // 这里不需要也不应该再包一层重试。
  const startMonitoringRef = useRef<(() => Promise<void>) | null>(null);
  useEffect(() => {
    if (!readMonitoringIntent()) return;
    void startMonitoringRef.current?.();
  }, []);

  const startMonitoring = useCallback(async () => {
    if (desiredModeRef.current === 'enrollment') {
      throw new Error('请先取消主人录入');
    }
    if (desiredModeRef.current === 'monitoring') return;
    desiredModeRef.current = 'monitoring';
    writeMonitoringIntent(true);
    setEnabled(true);
    setConnection('connecting');
    setPresence(initialPresence);
    setIdentity(initialIdentity);
    setMetrics(initialMetrics);
    setErrorMessage('');
    try {
      await window.xiaofei.camera.recognition.start({
        mode: 'monitoring',
        workstationId: WORKSTATION_ID,
      });
      await startCamera(selectedDeviceRef.current || undefined);
    } catch (error) {
      setErrorMessage(cameraErrorMessage(error));
      scheduleCameraRecovery();
    }
  }, [scheduleCameraRecovery, startCamera]);
  // 渲染期赋值,保证挂载 effect 执行时拿到的是本次渲染的 startMonitoring
  startMonitoringRef.current = startMonitoring;

  const stopMonitoring = useCallback(async () => {
    if (desiredModeRef.current !== 'monitoring') return;
    // 只有走到这里的手动关闭才清意图;应用退出的清理路径不经过本函数,
    // 因此「开着监测退出应用」重启后会自动恢复——这正是想要的语义
    writeMonitoringIntent(false);
    setEnabled(false);
    await stopSession();
    setPresence(initialPresence);
    setIdentity(initialIdentity);
    setErrorMessage('');
  }, [stopSession]);

  const startEnrollment = useCallback(async (displayName: string) => {
    if (enabled || desiredModeRef.current === 'monitoring') {
      throw new Error('请先关闭实时监测');
    }
    if (!displayName.trim()) throw new Error('请输入主人名称');
    desiredModeRef.current = 'enrollment';
    setEnrollment({ ...initialEnrollment, status: 'starting' });
    setConnection('connecting');
    setErrorMessage('');
    try {
      await startCamera(selectedDeviceRef.current || undefined);
      await window.xiaofei.camera.recognition.start({
        mode: 'enrollment',
        workstationId: WORKSTATION_ID,
        displayName: displayName.trim(),
      });
    } catch (error) {
      desiredModeRef.current = null;
      setEnrollment((current) => ({ ...current, status: 'error' }));
      setErrorMessage(cameraErrorMessage(error));
      stopCamera();
    }
  }, [enabled, startCamera, stopCamera]);

  const cancelEnrollment = useCallback(async () => {
    if (desiredModeRef.current === 'enrollment') await stopSession();
    setEnrollment(initialEnrollment);
    setErrorMessage('');
  }, [stopSession]);

  const selectDevice = useCallback(async (deviceId: string) => {
    selectedDeviceRef.current = deviceId;
    setSelectedDeviceId(deviceId);
    if (desiredModeRef.current !== null) await startCamera(deviceId);
  }, [startCamera]);

  const value = useMemo<CameraMonitoringValue>(() => ({
    enabled,
    stream,
    devices,
    selectedDeviceId,
    connection,
    presence,
    identity,
    metrics,
    enrollment,
    errorMessage,
    startMonitoring,
    stopMonitoring,
    startEnrollment,
    cancelEnrollment,
    selectDevice,
  }), [
    cancelEnrollment,
    connection,
    devices,
    enabled,
    enrollment,
    errorMessage,
    identity,
    metrics,
    presence,
    selectedDeviceId,
    selectDevice,
    startEnrollment,
    startMonitoring,
    stopMonitoring,
    stream,
  ]);

  return (
    <CameraMonitoringContext.Provider value={value}>
      {children}
      <video ref={hiddenVideoRef} autoPlay muted playsInline hidden />
    </CameraMonitoringContext.Provider>
  );
}

export const useCameraMonitoring = (): CameraMonitoringValue => {
  const value = useContext(CameraMonitoringContext);
  if (!value) throw new Error('useCameraMonitoring 必须在 CameraMonitoringProvider 内使用');
  return value;
};
