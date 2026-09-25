# Deployment files

| File | Where it goes | What it does |
|---|---|---|
| `kalshibot.service` | `/etc/systemd/system/` on a Linux VPS | runs the bot as a background service, restarts it on failure and after reboot |
| `com.kalshibot.plist` | `~/Library/LaunchAgents/` on macOS | runs the bot at login on your Mac |

Both files contain placeholder paths. Edit them before installing.

Full instructions are in section 5 of the main README.
