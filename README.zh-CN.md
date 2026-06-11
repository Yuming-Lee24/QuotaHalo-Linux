# QuotaHalo Linux

[English](README.md)

QuotaHalo Linux 是一个 GNOME 顶部状态栏小工具，可常驻显示 Copilot、Codex、Claude 的使用额度，以及本机系统状态。

## 显示内容

- GitHub Copilot AI Credits 用量
- OpenAI Codex 5 小时额度与 7 天额度
- Claude Code 5 小时额度与 7 天额度
- Claude Code 实时会话状态（工作中 / 等你回复 / 需要你 / 空闲），并支持桌面通知
- CPU、内存、GPU 使用率
- 网络下行/上行速度

## 数据来源

Codex 和 Claude 的额度直接来自当前登录账号的订阅信息，无需额外配置。

Copilot 需要 GitHub token 才能读取 AI Credits 用量。复制 `.env.example` 后填写：

```bash
GITHUB_TOKEN=your_github_personal_access_token_here
GITHUB_USERNAME=your_github_username_here
```

### 获取 GitHub Fine-Grained Personal Access Token

1. 打开 GitHub `Settings` > `Developer settings` > `Personal access tokens` > `Fine-grained tokens`。
2. 点击 `Generate new token`。
3. 填写一个名称，例如 `Copilot Usage Monitor`。
4. 在 `Repository access` 中选择 `Public Repositories (read-only)` 或 `All repositories`。QuotaHalo 只读取账号级 billing 信息，不会读取仓库内容。
5. 在 `Account permissions` 中找到 `Plan`，将其设置为 `Read-only`。
6. 点击 `Generate token`，并将生成的 token 复制到 `.env` 的 `GITHUB_TOKEN`。

注意：token 必须具备 `Plan: Read-only` 权限，否则无法读取 billing 和 usage 信息。

## Claude Code 会话状态

QuotaHalo 还能在额度胶囊上直接显示你正在运行的 Claude Code 会话的状态：

- 用一个彩色圆点汇总所有会话——**绿色** = 工作中，**蓝色** = 一轮结束、轮到你回复，**琥珀色** = 需要你处理（授权请求或在问你问题），**灰色** = 空闲（结束后走开）。圆点取最紧急的状态（需要你 > 工作中 > 轮到你 > 空闲），没有会话时隐藏。
- 点击胶囊会展开 **Claude Code Sessions** 列表，显示每个会话的标题、状态、当前工具，以及在该状态下停留的时长。（标题取 Claude Code 自己的会话名；在标题生成之前回退显示工作目录名。）
- 当某个会话从**工作中 → 需要你**、或从**工作中 → 已完成（轮到你）**切换时，会发送桌面通知。这样你就可以发起任务后切走，等它需要你或完成时再回来。

该功能基于 [Claude Code hooks](https://docs.claude.com/en/docs/claude-code/hooks) 实现。安装脚本会以「非破坏式合并」的方式把它们写入 `~/.claude/settings.json`：原有设置会备份到 `settings.json.quotahalo.bak`，其它键保持不变，重复运行安装脚本不会重复添加。每个事件都会运行一个很小的本地脚本（`claude_session_hook.py`），把会话状态记录到 `~/.cache/quotahalo/sessions/`。所有数据都不会离开你的机器。

随时可以移除这些 hooks：

```bash
python3 install_hooks.py --uninstall
```

## 安装

```bash
git clone https://github.com/eddy0619/QuotaHalo-Linux.git
cd QuotaHalo-Linux

# 可选：只有需要 Copilot 用量时才需要配置
cp .env.example .env
# 填写 .env

./install.sh
```

如果安装后顶部状态栏没有立即更新：

- X11：按 `Alt+F2`，输入 `r` 后回车。
- Wayland：注销后重新登录。

## 依赖

- Linux + GNOME Shell
- Python 3.10+

安装脚本会自动安装所需的 Python 依赖：`requests`、`python-dotenv`。

## License

本项目使用 MIT License，详情请参阅 [LICENSE](LICENSE)。

## Attribution

QuotaHalo Linux 是一个非官方项目，与 GitHub、Microsoft、OpenAI、Anthropic 或 GNOME Foundation 均无关联，也不代表上述任何组织的认可或背书。

特别感谢以下开源项目提供的参考：

- [burninc0de/copilot-usage-tracker](https://github.com/burninc0de/copilot-usage-tracker)
- [steipete/CodexBar](https://github.com/steipete/CodexBar)

- GitHub 和 GitHub Copilot 的名称、标识和相关图标归 GitHub, Inc. 和/或 Microsoft Corporation 所有。
- OpenAI、Codex 的名称、标识和相关图标归 OpenAI 所有。
- Claude、Anthropic 的名称、标识和相关图标归 Anthropic 所有。
- GNOME 和 GNOME Shell 的名称归 GNOME Foundation 及其相关权利方所有。
- 本项目中出现的第三方名称和图标仅用于标识对应的服务；这些第三方资产不在本项目的 MIT License 授权范围之内。
- Copilot 用量数据来自 GitHub REST Billing API；token 权限说明参考 GitHub 官方文档中对 Fine-grained Personal Access Token `Plan: Read-only` 权限的要求。

所有未特别列出的商标、服务名称和图标均归其各自权利方所有。
