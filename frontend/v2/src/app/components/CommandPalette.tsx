import { Fragment, useCallback, useEffect, useLayoutEffect, useRef, useState, type KeyboardEvent } from "react";
import { useNavigate } from "react-router-dom";

import { Icon, type IconName } from "@/shared/components/Icon";
import { ModalOverlay } from "@/shared/components/ModalOverlay";
import { requestModalClose } from "@/shared/ui/modalClose";
import {
  onCommandPaletteOpen,
  settingsCommands,
  taskCommands,
  type SettingsCommand,
  type TaskCommand,
} from "@/shared/lib/commands";

type Command = {
  group: "创建" | "页面导航" | "管理员";
  label: string;
  icon: IconName;
  keywords: string;
  adminOnly?: boolean;
  route?: "/files" | "/history" | "/shares" | "/profile";
  task?: TaskCommand;
  settings?: SettingsCommand;
};

const ALL_COMMANDS: Command[] = [
  { group: "创建", label: "添加下载任务", icon: "plus", task: "add", keywords: "任务 下载 新建" },
  { group: "创建", label: "批量添加任务", icon: "tasks", task: "batch", keywords: "任务 批量 链接" },
  { group: "创建", label: "上传种子文件", icon: "upload", task: "torrent", keywords: "种子 BT 文件" },
  { group: "页面导航", label: "打开文件管理", icon: "files", route: "/files", keywords: "文件 目录 打包 分享" },
  { group: "页面导航", label: "打开任务历史", icon: "clock", route: "/history", keywords: "历史 完成 失败 取消" },
  { group: "页面导航", label: "打开分享管理", icon: "share", route: "/shares", keywords: "分享 链接 提取码" },
  { group: "页面导航", label: "打开用户设置", icon: "user", route: "/profile", keywords: "个人 通知 密码 外部访问" },
  { group: "管理员", label: "打开基础设置", icon: "settings", settings: "basic", adminOnly: true, keywords: "设置 基础 系统" },
  { group: "管理员", label: "打开实时连接设置", icon: "link", settings: "realtime", adminOnly: true, keywords: "WebSocket 重连 实时 设置" },
  { group: "管理员", label: "打开安全与限流设置", icon: "shield", settings: "security", adminOnly: true, keywords: "安全 限流 凭证 设置" },
];

function filterCommands(commands: Command[], query: string) {
  const normalized = query.trim().toLocaleLowerCase();
  return commands.filter((command) =>
    !normalized || `${command.label} ${command.keywords}`.toLocaleLowerCase().includes(normalized),
  );
}

export function CommandPalette({ isAdmin }: { isAdmin: boolean }) {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const [trigger, setTrigger] = useState<HTMLElement | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const pendingCommandRef = useRef<Command | null>(null);

  const commands = ALL_COMMANDS.filter((command) => isAdmin || !command.adminOnly);
  const filteredCommands = filterCommands(commands, query);
  const selectedIndex = Math.min(activeIndex, Math.max(0, filteredCommands.length - 1));

  const openPalette = useCallback((invoker: HTMLElement | null) => {
    setTrigger(invoker);
    setQuery("");
    setActiveIndex(0);
    setOpen(true);
  }, []);

  useEffect(() => onCommandPaletteOpen(openPalette), [openPalette]);

  useEffect(() => {
    const handleKeyDown = (event: globalThis.KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey) || event.key.toLocaleLowerCase() !== "k") return;
      event.preventDefault();
      if (!open) {
        openPalette(document.querySelector<HTMLElement>('[data-action="open-command"]'));
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [open, openPalette]);

  useLayoutEffect(() => {
    if (!open) return;
    inputRef.current?.focus();
    inputRef.current?.select();
  }, [open]);

  const handleClose = useCallback(() => setOpen(false), []);

  useEffect(() => {
    if (open || pendingCommandRef.current === null) return;
    const command = pendingCommandRef.current;
    pendingCommandRef.current = null;
    if (command.route) {
      navigate(command.route);
      return;
    }
    if (command.task) {
      navigate("/tasks");
      taskCommands.request(command.task);
      return;
    }
    if (command.settings) {
      navigate(`/settings/${command.settings}`);
      settingsCommands.request(command.settings);
    }
  }, [open, navigate]);

  const runCommand = (command: Command) => {
    pendingCommandRef.current = command;
    requestModalClose(inputRef.current);
  };

  const handleInputKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowDown" && filteredCommands.length > 0) {
      event.preventDefault();
      setActiveIndex((selectedIndex + 1) % filteredCommands.length);
      return;
    }
    if (event.key === "ArrowUp" && filteredCommands.length > 0) {
      event.preventDefault();
      setActiveIndex((selectedIndex - 1 + filteredCommands.length) % filteredCommands.length);
      return;
    }
    if (event.key === "Enter" && filteredCommands.length > 0) {
      event.preventDefault();
      runCommand(filteredCommands[selectedIndex]);
    }
  };

  if (!open) return null;

  let previousGroup = "";
  return (
    <ModalOverlay
      onClose={handleClose}
      ariaLabel="快捷操作"
      className="modal-overlay command-dialog"
      contentClassName="command-shell"
      trigger={trigger}
      motionVariant="command"
    >
      <div className="command-input">
        <span><Icon name="search" /></span>
        <input
          ref={inputRef}
          type="search"
          autoComplete="off"
          aria-label="搜索操作"
          placeholder="输入操作名称"
          value={query}
          onChange={(event) => {
            const value = event.target.value;
            setQuery(value);
            setActiveIndex((current) =>
              Math.min(current, Math.max(0, filterCommands(commands, value).length - 1)),
            );
          }}
          onKeyDown={handleInputKeyDown}
        />
        <kbd>Esc</kbd>
      </div>
      <div className="command-results">
        {filteredCommands.length === 0 ? (
          <div className="command-empty">没有匹配的操作</div>
        ) : filteredCommands.map((command, index) => {
          const showGroup = command.group !== previousGroup;
          previousGroup = command.group;
          return (
            <Fragment key={command.label}>
              {showGroup ? <div className="command-group-label">{command.group}</div> : null}
              <button
                className={`command-item${index === selectedIndex ? " active" : ""}`}
                type="button"
                onMouseEnter={() => setActiveIndex(index)}
                onClick={() => runCommand(command)}
              >
                <Icon name={command.icon} />
                <span>{command.label}</span>
                <Icon name="chevron-right" className="icon-sm" />
              </button>
            </Fragment>
          );
        })}
      </div>
    </ModalOverlay>
  );
}
