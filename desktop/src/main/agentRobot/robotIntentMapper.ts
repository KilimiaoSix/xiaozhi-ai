import type {
  AgentTaskSnapshot,
  RobotActionIntent,
} from '../../modules/features/coding-agent-status/agent-hooks/contracts';

/**
 * `/xiaozhi/event/push` 的请求体。字段含义见 AGENTS.md「关键接口契约」。
 * device_id 与 text 必填，其余可选。
 *
 * kind / task_key / source 是给服务端离席台账（返岗汇总）用的：
 * 同一任务的多次状态变化靠 task_key 折叠成一条，「Codex 完成了 N 个任务」
 * 的来源名靠 source。设备侧不消费这三个字段。
 */
export interface RobotPushBody {
  device_id: string;
  text: string;
  emotion: string;
  status: string;
  speak: boolean;
  silent?: boolean;
  action?: string;
  restore_after?: number;
  kind?: string;
  task_key?: string;
  source?: string;
}

const MAX_TITLE = 20;
const MAX_DETAIL = 24;
const FALLBACK_TITLE = '编码任务';

/** 意图动作 → 服务端台账事件类型（away_ledger 的 kind 枚举）。 */
const LEDGER_KINDS: Record<RobotActionIntent['action'], string> = {
  task_completed: 'agent_completed',
  task_failed: 'agent_failed',
  needs_user: 'agent_needs_user',
  quiet_companion: 'generic',
};

/** 返岗汇总里的来源名。播报要念出来，用产品名而不是内部标识。 */
const SOURCE_NAMES: Record<string, string> = {
  codex: 'Codex',
  'claude-code': 'Claude Code',
  workbuddy: 'WorkBuddy',
};

const ledgerFields = (
  intent: Pick<RobotActionIntent, 'action'> & Partial<RobotActionIntent>,
  task: AgentTaskSnapshot | undefined,
): Pick<RobotPushBody, 'kind' | 'task_key' | 'source'> => ({
  kind: LEDGER_KINDS[intent.action],
  ...(intent.taskKey ? { task_key: intent.taskKey } : {}),
  ...(task?.source ? { source: SOURCE_NAMES[task.source] ?? task.source } : {}),
});

const clip = (value: string | undefined, max: number): string => {
  const text = (value ?? '').replace(/\s+/g, ' ').trim();
  if (!text) return '';
  return text.length > max ? `${text.slice(0, max)}…` : text;
};

/**
 * 把一条机器人意图翻译成推送请求体。
 *
 * 表现按 AGENTS.md「核心体验」定：运行中安静陪伴、完成抬头点头、失败低头、
 * 等待介入歪头提醒。emotion 本身就会驱动舵机（happy→HeadUp、sad→HeadDown），
 * 所以只在需要额外手势时才叠 action。
 *
 * 文案只用任务标题与错误/等待原因——快照里没有改动文件数、测试数这类字段，
 * 编造出来在演示里一追问就穿帮。
 */
export const mapIntentToPush = (
  deviceId: string,
  intent: RobotActionIntent,
  task: AgentTaskSnapshot | undefined,
): RobotPushBody => {
  const title = clip(task?.title, MAX_TITLE) || FALLBACK_TITLE;
  const ledger = ledgerFields(intent, task);

  switch (intent.action) {
    case 'task_completed':
      return {
        device_id: deviceId,
        text: `${title} 完成了`,
        emotion: 'happy',
        status: '任务完成',
        action: 'nod',
        speak: true,
        restore_after: 10,
        ...ledger,
      };

    case 'task_failed': {
      const reason = clip(task?.error, MAX_DETAIL);
      return {
        device_id: deviceId,
        text: reason ? `${title} 失败了：${reason}` : `${title} 失败了`,
        emotion: 'sad',
        status: '任务失败',
        speak: true,
        restore_after: 12,
        ...ledger,
      };
    }

    case 'needs_user': {
      const reason = clip(task?.needsUserReason, MAX_DETAIL);
      return {
        device_id: deviceId,
        text: reason ? `${title} 需要你看一下：${reason}` : `${title} 需要你看一下`,
        emotion: 'confused',
        status: '待处理',
        action: 'look_left',
        speak: true,
        // 不设 restore_after：等待介入是唯一必须持续可见的态，
        // 只应由后续同任务的完成或失败事件清场，不该被定时器抹掉。
        ...ledger,
      };
    }

    case 'quiet_companion':
    default:
      return {
        device_id: deviceId,
        text: title,
        emotion: 'thinking',
        status: '进行中',
        speak: false,
        // 运行中一次任务会触发多次，既不出声也不响提示音，
        // 免得占着 TTS 通道、跟紧随其后的终态播报抢麦。
        silent: true,
        ...ledger,
      };
  }
};

/**
 * 多个任务在同一窗口内落到同一终态时，合并成一条播报。
 *
 * 连播三句「X 完成了」在真机上会互相撞：设备一次只播一句，后到的会被服务端
 * 按忙态降级成提示音，等于白发。合并成「3 个任务完成了」既说得完整，也更像人话。
 */
export const mapMergedIntentToPush = (
  deviceId: string,
  action: RobotActionIntent['action'],
  count: number,
): RobotPushBody => {
  const single = mapIntentToPush(deviceId, { action } as RobotActionIntent, undefined);

  switch (action) {
    case 'task_completed':
      return { ...single, text: `${count} 个任务完成了` };
    case 'task_failed':
      return { ...single, text: `${count} 个任务失败了` };
    case 'needs_user':
      return { ...single, text: `${count} 个任务需要你看一下` };
    case 'quiet_companion':
    default:
      return { ...single, text: `${count} 个任务进行中` };
  }
};
