# QuotaHalo Linux

[English](README.md)

QuotaHalo Linux 是一个 GNOME 顶部状态栏小工具，可常驻显示 Copilot、Codex、Claude 的使用额度，以及本机系统状态。

## 显示内容

- GitHub Copilot AI Credits 用量
- OpenAI Codex 5 小时额度与 7 天额度
- Claude Code 5 小时额度与 7 天额度
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
