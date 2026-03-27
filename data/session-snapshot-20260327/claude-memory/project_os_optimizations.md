---
name: OS Optimizations 2026-03-27
description: All system optimizations applied to M1 Ubuntu/GNOME — 38 phases, 39G recovered, boot halved
type: project
---

## Session 2026-03-27 — Optimizations Applied

### Disk: 293G → 253G used (39G recovered)
- 50 snaps removed (91G → 7G): JetBrains IDEs, O3DE, Wine, CUPS, Chromium, Eclipse...
- Firefox/VS Code/Thunderbird: snap → native deb
- Docker volumes pruned: 69G
- Journal limited: 500M/7 days
- Old kernel 6.17.0-14 purged
- JetBrains Toolbox installed at `~/.local/share/JetBrains/Toolbox/`

### Services disabled (15+)
dundee, switcheroo-control, kerneloops, thermald, cloud-init(x4), avahi-daemon, cups, wpa_supplicant, plymouth-quit-wait, gpu-manager, apt-daily timers, tracker-miner-fs-3

### Boot: ~67s → ~35s estimated
- plymouth-quit-wait disabled (-22s)
- apt-daily timers disabled (-5s)
- gpu-manager disabled (-2.4s)

### Kernel (GRUB): mitigations=off nowatchdog transparent_hugepage=madvise
Applied in `/etc/default/grub`, update-grub done. Active after reboot.

### New services created
- `jarvis-gpu-init.service` — GPU exclusive compute mode for inference GPUs
- `jarvis-oom-protect.service` — LM Studio/Ollama protected from OOM, Chrome sacrificed first
- `jarvis-maintenance.timer` — Weekly cleanup Sunday 5am

### GNOME
- Animations: OFF
- Key repeat: ON (250ms delay, 30ms interval)
- Terminal Ctrl+C/V/A/F configured
- Search providers: external disabled
- Idle lock: disabled
- Alt+Tab: current workspace only

### Network
- IPv6: disabled
- DNS: Cloudflare+Google over TLS (opportunistic)
- UFW firewall: enabled, LAN-only ports for JARVIS services
- Sysctl: optimized (buffers, inotify, dirty ratio)

### NVMe
- read_ahead: 256KB (was 128)
- udev rule: `/etc/udev/rules.d/60-nvme-perf.rules`

### ZRAM: 25% → 30% (active after reboot)

### /tmp: tmpfs 8G (active after reboot)

### BrowserOS session persistence fix
- Preferences: exit_type=Normal, restore_on_startup=1, cookies kept
- Watchdog patched: fix_preferences before start/stop
- Shutdown script: `~/.browseros/shutdown-clean.sh`

### nvidia-utils-590 reinstalled (was removed by autoremove)
