# QuotaHalo Linux

[English](README.md)

QuotaHalo Linux 是一个 GNOME 顶部状态栏监控插件，用来常驻显示 AI 助手额度和本机系统状态。

它把 GitHub Copilot、OpenAI Codex、Claude Code、CPU、内存、GPU、网络上下行，以及可选的 FlClash 公网 IP 属地整合到一个 GNOME Shell 扩展里。

## 显示内容

- GitHub Copilot AI Credits 用量
- OpenAI Codex 5 小时 session 用量
- OpenAI Codex 7 天用量
- Claude Code 5 小时 session 用量
- Claude Code 7 天用量
- CPU、内存、GPU、网络下行/上行速率
- 检测到 FlClash 时，显示公网 IP 的国家旗帜和国家简称

AI 额度组件会放在 GNOME 时间栏右侧，避免挤压居中的时间栏。系统监控组件会放在左侧区域靠右的位置。

点击顶部组件可以展开详情。

## 数据来源

### Copilot

Copilot 用量来自 GitHub Billing API，缓存写入：

```text
~/.cache/copilot-usage/status.json
```

需要在本项目目录创建 `.env`：

```bash
GITHUB_TOKEN=your_github_personal_access_token_here
GITHUB_USERNAME=your_github_username_here
GITHUB_AI_CREDITS_LIMIT=1500
```

`.env` 只用于本机，不要提交到 Git。

### Codex

Codex 用量来自本机 Codex CLI 数据：

```text
~/.codex/config.toml
~/.codex/auth.json
~/.codex/sessions/**/*.jsonl
```

插件会读取 Codex CLI 记录的 rate limit 信息，用于显示 5 小时 session 和 7 天用量。

### Claude

Claude 优先读取 Claude Code OAuth 凭据：

```text
~/.claude/.credentials.json
```

当 OAuth usage 不可用时，会 fallback 到 `claude /usage`。这条 fallback 可能让 Claude Code 写入一条查询对话，所以 QuotaHalo 会把纯 `/usage` 或 `usage` 查询 transcript 移出 Claude 历史，放到：

```text
~/.cache/quotahalo/claude-usage-query-trash/
```

## 安装

```bash
git clone https://github.com/eddy0619/QuotaHalo-Linux.git
cd QuotaHalo-Linux

cp .env.example .env
# 如果需要 Copilot 用量，编辑 .env

./install.sh
```

如果安装后顶部状态栏没有立即刷新：

- X11：按 `Alt+F2`，输入 `r`，回车。
- Wayland：注销后重新登录。

## 手动命令

手动刷新 Codex 和 Claude：

```bash
python3 quota_halo_status.py --refresh-once
```

手动刷新 Copilot：

```bash
python3 copilot_status_service.py --once
```

清理历史中旧的 Claude usage 查询 transcript：

```bash
python3 quota_halo_status.py --cleanup-claude-usage-queries
```

## 安装后的服务

`install.sh` 会安装一个 GNOME 扩展和两个 user systemd 服务：

```text
quotahalo@local
quotahalo-refresh.timer
copilot-usage.service
```

查看服务状态：

```bash
systemctl --user status quotahalo-refresh.timer
systemctl --user status copilot-usage.service
```

## 项目结构

```text
assets/                               GNOME 扩展使用的图标
gnome-extension/quotahalo@local/      GNOME Shell 扩展
systemd/                              user service 模板
quota_halo_status.py                  Codex 和 Claude 缓存刷新器
copilot_status_service.py             Copilot 缓存刷新器
install.sh                            依赖和扩展安装入口
install-gnome-extension.sh            GNOME 扩展和 systemd 服务安装脚本
```

## 依赖

- Linux + GNOME Shell
- Python 3.10+
- `requests`
- `python-dotenv`

## 说明

- `.env` 已被 `.gitignore` 忽略，不会提交。
- 旧的 `copilot-usage-tracker` 独立工程不再需要。
- 旧的 GUI tray app 不再需要。
