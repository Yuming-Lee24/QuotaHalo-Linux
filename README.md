# QuotaHalo Linux

[中文说明](README.zh-CN.md)

QuotaHalo Linux is a GNOME top-bar widget that keeps your Copilot, Codex, and Claude usage quotas on screen, along with local system status.

## What It Shows

- GitHub Copilot AI Credits usage
- OpenAI Codex 5-hour and 7-day quotas
- Claude Code 5-hour and 7-day quotas
- Live Claude Code session status (working / awaiting reply / needs input / idle), with desktop alerts
- CPU, memory, and GPU usage
- Network download/upload speed

## Data Sources

Codex and Claude quotas come directly from the subscription information of the currently signed-in account, with no extra configuration required.

Copilot requires a GitHub token to read AI Credits usage. Copy `.env.example` and fill it in:

```bash
GITHUB_TOKEN=your_github_personal_access_token_here
GITHUB_USERNAME=your_github_username_here
```

### Getting a GitHub Fine-Grained Personal Access Token

1. Open GitHub `Settings` > `Developer settings` > `Personal access tokens` > `Fine-grained tokens`.
2. Click `Generate new token`.
3. Give it a name, for example `Copilot Usage Monitor`.
4. Under `Repository access`, choose `Public Repositories (read-only)` or `All repositories`. QuotaHalo only reads account-level billing information and does not read repository contents.
5. Under `Account permissions`, find `Plan` and set it to `Read-only`.
6. Click `Generate token`, then copy the generated token into `GITHUB_TOKEN` in `.env`.

Note: the token must have the `Plan: Read-only` permission, otherwise it cannot read billing and usage information.

## Claude Code Session Status

QuotaHalo can also show what your running Claude Code sessions are doing, right on the usage pill:

- A single colored dot summarizes every session — **green** = working, **blue** = finished its turn (your turn to reply), **amber** = needs your input (a permission prompt or a question), **grey** = idle (away after finishing). The dot shows the most urgent state (needs-input > working > awaiting-reply > idle) and is hidden when no session is running.
- Click the pill to open the **Claude Code Sessions** list, showing each session's title, state, current tool, and time in that state. (The title is Claude Code's own session name; it falls back to the working-directory name until a title is generated.)
- You get a desktop notification when a session goes from **working → needs input** or **working → finished (your turn)**, so you can start a task, switch away, and get pinged when it needs you or is done.

This is powered by [Claude Code hooks](https://docs.claude.com/en/docs/claude-code/hooks). The installer registers them in `~/.claude/settings.json` with a non-destructive merge: your existing settings are backed up to `settings.json.quotahalo.bak`, sibling keys are preserved, and re-running the installer is a no-op. Each event runs a tiny local script (`claude_session_hook.py`) that records the session's state under `~/.cache/quotahalo/sessions/`. Nothing leaves your machine.

Remove the hooks at any time:

```bash
python3 install_hooks.py --uninstall
```

## Install

```bash
git clone https://github.com/eddy0619/QuotaHalo-Linux.git
cd QuotaHalo-Linux

# Optional: only needed if you want Copilot usage
cp .env.example .env
# Fill in .env

./install.sh
```

If the top bar does not update immediately after installation:

- X11: press `Alt+F2`, type `r`, and press Enter.
- Wayland: log out and back in.

## Requirements

- Linux + GNOME Shell
- Python 3.10+

The install script automatically installs the required Python dependencies: `requests` and `python-dotenv`.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.

## Attribution

QuotaHalo Linux is an unofficial project. It is not affiliated with, endorsed by, or sponsored by GitHub, Microsoft, OpenAI, Anthropic, or the GNOME Foundation.

Special thanks to the following open-source projects for reference:

- [burninc0de/copilot-usage-tracker](https://github.com/burninc0de/copilot-usage-tracker)
- [steipete/CodexBar](https://github.com/steipete/CodexBar)

- The GitHub and GitHub Copilot names, logos, and related icons are owned by GitHub, Inc. and/or Microsoft Corporation.
- The OpenAI and Codex names, logos, and related icons are owned by OpenAI.
- The Claude and Anthropic names, logos, and related icons are owned by Anthropic.
- The GNOME and GNOME Shell names are owned by the GNOME Foundation and its respective rights holders.
- Third-party names and icons that appear in this project are used solely to identify the corresponding services; these third-party assets are not covered by this project's MIT License.
- Copilot usage data comes from the GitHub REST Billing API; the token permission requirements follow the `Plan: Read-only` permission for Fine-grained Personal Access Tokens described in GitHub's official documentation.

All trademarks, service names, and icons not explicitly listed remain the property of their respective rights holders.
