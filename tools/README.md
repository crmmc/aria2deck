# Aria2Deck 运维小工具

本目录存放**独立运维脚本**，不依赖后端包导入，可直接在容器/宿主机用系统 Python 运行。

## 工具列表

| 脚本 | 用途 |
|------|------|
| `scan_unmanaged_download_dir.py` | 扫描下载目录中**不在 Aria2Deck 管理布局内**的文件，以 tree 展示并标注每一级大小 |

---

## `scan_unmanaged_download_dir.py`

### 背景

Aria2Deck 只统一管理下载目录下的部分布局：

```text
<download_dir>/
├── store/                 # 成品内容寻址存储（整棵视为管理区）
└── downloading/
    ├── <task_id>/         # 进行中任务目录
    └── pack_<task_id>/    # 打包临时目录
```

根目录杂物（如手工测试文件、历史目录、误放的日志/DB）**不会**被系统自动清理。  
本工具用于快速发现这些「未管理」对象。

### 判定规则

| 路径 | 是否扫描报告 |
|------|----------------|
| `store/**` | 否（管理区，整棵跳过） |
| `downloading/<纯数字>/` | 否（任务目录） |
| `downloading/pack_<纯数字>/` | 否（打包目录） |
| `.aria2deck-write-test` | 否（启动写探针） |
| 其他所有文件/目录 | **是**（未管理） |

说明：

- 本工具按**布局**判断，不连数据库；`store` 内孤儿对象由应用启动 orphan cleanup 处理，不在此工具范围。
- 软链接按文件节点展示；目录大小为未管理子树合计。

### 用法

```bash
# 使用环境变量（容器内常见）
export ARIA2C_DOWNLOAD_DIR=/Downloads/aria2deck
python3 tools/scan_unmanaged_download_dir.py

# 或显式指定
python3 tools/scan_unmanaged_download_dir.py --download-dir /Downloads/aria2deck

# 附绝对路径
python3 tools/scan_unmanaged_download_dir.py --download-dir /Downloads/aria2deck --show-paths

# 额外输出 JSON 汇总行
python3 tools/scan_unmanaged_download_dir.py --download-dir /Downloads/aria2deck --json-summary
```

容器内示例：

```bash
docker exec -it <container> \
  python3 /app/tools/scan_unmanaged_download_dir.py \
  --download-dir /Downloads/aria2deck
```

若镜像未包含本仓库 `tools/`，可把脚本拷进容器后执行。

### 输出示例

```text
/Downloads/aria2deck  [105.0MiB unmanaged]
├── 3/  [dir]  0B
├── app.db  [file]  440.0KiB
├── aria2deck.log  [file]  12.3MiB
└── test  [file]  100.0MiB

汇总: unmanaged_dirs=1 unmanaged_files=3 total_size=105.0MiB (110100480 bytes)
说明: store/** 与 downloading/<id|pack_id>/ 视为系统管理布局，不在本扫描结果中列出。
```

### 注意

- **只读扫描**，不会删除任何文件。
- 删除前请人工确认；切勿直接删 `app.db` 或仍在使用的路径。
- 需要 Python 3.10+（仅标准库）。
