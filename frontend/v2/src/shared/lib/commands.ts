export type TaskCommand = "add" | "batch" | "torrent";
export type SettingsCommand = "basic" | "realtime" | "security";

const OPEN_COMMAND_EVENT = "v2:open-command-palette";

function createPageCommandBridge<Command extends string>(eventName: string) {
  let pending: Command | null = null;

  return {
    request(command: Command) {
      pending = command;
      window.dispatchEvent(new CustomEvent<Command>(eventName, { detail: command }));
    },
    subscribe(handler: (command: Command) => void) {
      const listener = (event: Event) => {
        pending = null;
        handler((event as CustomEvent<Command>).detail);
      };
      window.addEventListener(eventName, listener);
      // 导航和目标页挂载可能发生在同一帧，暂存一次以免命令丢失。
      if (pending !== null) {
        const command = pending;
        pending = null;
        handler(command);
      }
      return () => window.removeEventListener(eventName, listener);
    },
  };
}

export const taskCommands = createPageCommandBridge<TaskCommand>("v2:task-command");
export const settingsCommands = createPageCommandBridge<SettingsCommand>("v2:settings-command");

export function openCommandPalette(trigger: HTMLElement) {
  window.dispatchEvent(new CustomEvent<HTMLElement>(OPEN_COMMAND_EVENT, { detail: trigger }));
}

export function onCommandPaletteOpen(handler: (trigger: HTMLElement) => void) {
  const listener = (event: Event) => handler((event as CustomEvent<HTMLElement>).detail);
  window.addEventListener(OPEN_COMMAND_EVENT, listener);
  return () => window.removeEventListener(OPEN_COMMAND_EVENT, listener);
}
