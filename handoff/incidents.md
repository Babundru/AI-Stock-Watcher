# Incidents

Past production problems on the VM: what it looked like, how it was
diagnosed, what was changed. Read this first if the VM is frozen,
unreachable, or the serial console is complaining about "the journal".

No VM identifiers (IPs, project ID, instance name) in here - this file is
committed. Commands use `<instance>` / `<zone>` placeholders: substitute
them before pasting (bash reads a literal `<zone>` as a redirect).

---

## 2026-09: VM froze after 2-3 days of uptime (memory livelock)

### What it looked like

- Ran fine for the first few days after deploy.
- Then: an error "about the journal" on the console, Tailscale stopped
  working, SSH (both over Tailscale and the Console's browser SSH) failed,
  the machine looked frozen.
- Browser SSH said *"We are unable to connect to the VM on port 22 ...
  ensure that VM has a firewall rule that allows TCP ingress"*. **That was
  misleading** - `default-allow-ssh` (tcp:22 from 0.0.0.0/0) existed and
  was enabled the whole time. The Console shows that text for any failed
  connect to :22, whatever the cause.
- Felt like "it keeps turning back on for a few hours at a time". In fact
  the VM never rebooted by itself - see the timeline.

### Timeline (UTC)

| When | What |
|---|---|
| Aug 31 18:46 | VM created |
| Sep 4 03:13 | `compute.instances.migrateOnHostMaintenance` (GCE live migration) |
| Sep 4 07:04 | First failed SSH attempts |
| Sep 4 12:38 | Manual `reset` #1 |
| Sep 6 13:42 | Manual `reset` #2 (kernel boot 36s later matches the serial log's uptime counter exactly) |
| Sep 9 ~06:01 | `Under memory pressure` begins, at uptime 2d 16h |
| Sep 9 20:55 | SSH fails again |
| Sep 10 | `stop` / `start`, fixes below written |

Time-to-failure across the three runs: ~3.5 days, ~2.0 days, ~3.3 days.
That consistency is what pointed at something growing with uptime rather
than a one-off event. The live migration came 4h before the first failure
but there was no migration before the other two, so it's treated as a
coincidence.

### Evidence

From `gcloud compute instances get-serial-port-output` of the frozen VM:

```
systemd-journald[267]: Under memory pressure, flushing caches.     <- x426, every ~75s for hours
systemctl[...]: Failed to get load state of systemd-networkd.service: Connection timed out
systemd-resolved[312]: Under memory pressure, flushing caches.
google_guest_agent_manager[851]: Plugin health check failed ... DeadlineExceeded
```

- The "journal error" was that journald line.
- **No `oom-kill` / `Killed process` anywhere.** The kernel never killed
  anything.
- Exactly one app PID (`python[...]`) in the whole buffer, and no systemd
  restart messages: the service didn't crash or restart. (The buffer only
  held ~10 hours of the 3-day boot, so this is solid for that window only.)
- `gcloud compute operations list` showed no `reset`/`hostError`/
  `automaticRestart` except the two manual resets. A guest-initiated
  reboot wouldn't show up there, but the serial uptime counter rules that
  out too.

### Root cause

1GB RAM, no swap. As the process grew, the kernel didn't OOM-kill it. It
went into **permanent reclaim** instead: constantly dropping page cache,
never quite failing an allocation. The VM stayed `RUNNING`, but sshd
couldn't fork, `tailscaled` was starved, `systemctl` couldn't reach PID 1,
and the guest agent missed its health checks, so metadata SSH keys stopped
refreshing. Only a hard reset or stop/start recovers from that. An OOM
kill would have been far better: systemd would have restarted the app in
seconds with the box intact.

What was growing, per the code: **the set of open watches, and the price
fetch over it.**
- `watch_manager._trim()` never drops OPEN watches; WEEKS horizons run 21
  days; with `STOP_LOSS_PCT` set, a losing watch was re-postponed
  indefinitely.
- Every 5 minutes `_check_watches` passed *every* open ticker to one
  `yf.download(interval="1m", prepost=True)` (~960 rows x 6 cols per
  ticker, all built before the concat).
- If Yahoo rate-limited that and returned empty, every ticker fell through
  to a per-symbol `.info` fetch, sequentially, on the scan thread.

**Not directly proven:** we never got `free -h` or per-process RSS off
the frozen box. The app is the only large, growing process there, and
that's the only cost in the codebase that scales with uptime. The new
`Memory in use` log line is how to confirm it.

### Red herrings

- **`IPv4: martian source 10.x.x.2 from 10.x.x.1, on dev ens4`** every
  ~6s, plus some from public IPs. This is normal GCE noise (the gateway's
  ARP broadcasts hitting reverse-path logging). It was harmless, but it
  made up 2,356 of the 9,583 serial lines and buried the real messages.
  It's now silenced by sysctl.
- The Console's firewall message - see above.
- Disk full - ruled out. The app writes only small, bounded JSON files.

### What changed

Code:
- `price_lookup.py`: `yf.download` chunked to `MAX_BATCH=25`. The
  fallback is capped at `MAX_FALLBACK=25` and uses `fast_info` only
  (`.info` removed).
- `watch_manager.py`: `MAX_OPEN_DAYS=30`, `MAX_POSTPONEMENTS=14`,
  `over_age_limit()` (handles naive and aware timestamps), and
  `postpone_watch` returns `None` once the budget is spent.
- `main.py`: new close reasons `max_age` / `max_postponed`,
  `_release_memory()` after the watch check, and a per-cycle
  `Memory in use: N MB` log line.
- `cloud_providers.py`: `_log` no longer duplicates to stdout/journal when
  a callback exists.
- `notifier.py`, `web/index.html`: wording/labels for the two new reasons.

Deployment (`DEPLOY.md`, applied by hand on the VM):
- **zram** swap (`zram-tools`, zstd, 50%) instead of a swapfile. A
  swapfile on a 30GB pd-standard disk (~45 write / 22 read IOPS) makes the
  livelock worse.
- systemd unit: `MemoryHigh=420M`, `MemoryMax=550M`, `OOMPolicy=stop`,
  `RestartSec=30`, a start-rate limit, `Nice=5`,
  `IOSchedulingClass=idle`. **`MemoryMax` is the key line.** With it, the
  kernel kills only the app, and sshd/tailscaled stay reachable.
- `log_martians=0` in `/etc/sysctl.d/99-quiet.conf`.
- journald capped at `SystemMaxUse=200M` / `RuntimeMaxUse=50M`.

### Open follow-ups

- Watch the `Memory in use` line over the first few days after deploying.
  If it stays flat, the diagnosis holds. If it still climbs, the growth is
  somewhere else. Take per-process RSS (`ps -eo rss,comm --sort=-rss |
  head`) while it's high.
- `default-allow-rdp` (tcp:3389 from anywhere) is open on a Linux VM that
  never uses it. Delete it.
- A daily disk snapshot runs at 02:46 UTC (resource policy attached at
  creation). It's fine to keep, but it's scheduled read load on a
  low-IOPS disk.

### If it happens again - diagnose from outside, before resetting

A reset destroys the evidence. From Cloud Shell:

```bash
gcloud compute instances list                                   # RUNNING? which zone?
gcloud compute operations list --filter="targetLink~<instance>" \
  --sort-by=~startTime --limit=40 \
  --format="table(operationType,status,startTime,statusMessage)"   # newest first!
gcloud compute instances get-serial-port-output <instance> --zone=<zone> --port=1 > serial.txt
grep -c "Under memory pressure" serial.txt
grep -inE "oom-kill|killed process|panic|hung task|No space left|read-only file system" serial.txt
grep -v -e martian -e "ll header" serial.txt | tail -120      # the real log, minus the noise
```

Then use `stop` / `start` rather than `reset`: `stop` sends an ACPI
shutdown first, so the guest can flush disk buffers. Neither one wipes
the disk. An ephemeral external IP changes on stop/start, which doesn't
matter over Tailscale.
