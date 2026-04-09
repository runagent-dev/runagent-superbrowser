/**
 * Event system from nanobrowser.
 *
 * Pub-sub pattern for task/step/action lifecycle events.
 * Used to feed real-time status to UI or external systems.
 */

export enum ExecutionState {
  TASK_START = 'TASK_START',
  TASK_OK = 'TASK_OK',
  TASK_FAIL = 'TASK_FAIL',
  TASK_PAUSE = 'TASK_PAUSE',
  TASK_RESUME = 'TASK_RESUME',
  TASK_CANCEL = 'TASK_CANCEL',
  STEP_START = 'STEP_START',
  STEP_OK = 'STEP_OK',
  STEP_FAIL = 'STEP_FAIL',
  ACT_START = 'ACT_START',
  ACT_OK = 'ACT_OK',
  ACT_FAIL = 'ACT_FAIL',
  HUMAN_INPUT_NEEDED = 'HUMAN_INPUT_NEEDED',
  HUMAN_INPUT_RECEIVED = 'HUMAN_INPUT_RECEIVED',
}

export enum Actor {
  SYSTEM = 'SYSTEM',
  NAVIGATOR = 'NAVIGATOR',
  PLANNER = 'PLANNER',
}

export interface ExecutionEvent {
  actor: Actor;
  state: ExecutionState;
  details: string;
  timestamp: number;
}

export type EventCallback = (event: ExecutionEvent) => void;

export class EventManager {
  private callbacks: EventCallback[] = [];

  subscribe(callback: EventCallback): void {
    this.callbacks.push(callback);
  }

  unsubscribe(callback: EventCallback): void {
    this.callbacks = this.callbacks.filter((cb) => cb !== callback);
  }

  emit(actor: Actor, state: ExecutionState, details: string): void {
    const event: ExecutionEvent = {
      actor,
      state,
      details,
      timestamp: Date.now(),
    };
    for (const cb of this.callbacks) {
      try {
        cb(event);
      } catch (err) {
        console.error('Event callback error:', err);
      }
    }
  }

  clearAll(): void {
    this.callbacks = [];
  }
}
