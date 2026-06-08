# QuotaHalo Linux

[中文说明](README.zh-CN.md)

QuotaHalo Linux is a GNOME Shell top-bar monitor for AI assistant usage and local system status.

It combines GitHub Copilot, OpenAI Codex, Claude Code, CPU, memory, GPU, network throughput, and optional FlClash IP location into one lightweight GNOME extension.

## What It Shows

- GitHub Copilot AI Credits usage
- OpenAI Codex 5h session usage
- OpenAI Codex 7d usage
- Claude Code 5h session usage
- Claude Code 7d usage
- CPU, memory, GPU, and network down/up rates
- FlClash public IP country flag and country code when FlClash is detected

The AI usage group is placed on the right side of the GNOME clock so the clock stays centered. The system monitor is placed near the right edge of the left panel area.

## Data Sources

### Copilot

Copilot usage is fetched from the GitHub Billing API and written to:

```text
~/.cache/copilot-usage/status.json
```

Configure it with a local `.env` file:

```bash
GITHUB_TOKEN=your_github_personal_access_token_here
GITHUB_USERNAME=your_github_username_here
GITHUB_AI_CREDITS_LIMIT=1500
```

### Codex

Codex usage is read from local Codex CLI data:

```text
~/.codex/config.toml
~/.codex/auth.json
~/.codex/sessions/**/*.jsonl
```

### Claude

Claude usage is read from Claude Code OAuth credentials when available:

```text
~/.claude/.credentials.json
```

If OAuth usage is unavailable, QuotaHalo can fall back to `claude /usage`. When that fallback creates a pure `/usage` or `usage` transcript, QuotaHalo moves it out of Claude history into:

```text
~/.cache/quotahalo/claude-usage-query-trash/
```

## Install

```bash
git clone https://github.com/eddy0619/QuotaHalo-Linux.git
cd QuotaHalo-Linux

cp .env.example .env
# Edit .env if you want Copilot usage.

./install.sh
```

If GNOME does not reload the extension immediately:

- On X11: press `Alt+F2`, type `r`, and press Enter.
- On Wayland: log out and back in.

## Manual Commands

Refresh Codex and Claude once:

```bash
python3 quota_halo_status.py --refresh-once
```

Refresh Copilot once:

```bash
python3 copilot_status_service.py --once
```

Clean old Claude usage-query transcripts:

```bash
python3 quota_halo_status.py --cleanup-claude-usage-queries
```

## Installed Services

`install.sh` installs a single GNOME extension and two user-level systemd services:

```text
quotahalo@local
quotahalo-refresh.timer
copilot-usage.service
```

Check service state with:

```bash
systemctl --user status quotahalo-refresh.timer
systemctl --user status copilot-usage.service
```

## Project Layout

```text
assets/                               icons used by the GNOME extension
gnome-extension/quotahalo@local/      GNOME Shell extension
systemd/                              user service templates
quota_halo_status.py                  Codex and Claude cache refresher
copilot_status_service.py             Copilot cache refresher
install.sh                            dependency and extension installer
install-gnome-extension.sh            GNOME extension and service installer
```

## Requirements

- Linux with GNOME Shell
- Python 3.10+
- `requests`
- `python-dotenv`

## Notes

- `.env` is local-only and must not be committed.
- The old standalone `copilot-usage-tracker` project is not required.
- The old GUI tray app is not required.
