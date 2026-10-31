# Incidents

Past production problems on the VM: what it looked like, how it was
diagnosed, what was changed. Read this first if the VM is frozen,
unreachable, or the serial console is complaining about "the journal".

No VM identifiers (IPs, project ID, instance name) in here - this file is
committed. Commands use `<instance>` / `<zone>` placeholders: substitute
them before pasting (bash reads a literal `<zone>` as a redirect).

---

## 2026-10-31: app frozen while `active` - the leak found

### What it looked like

`systemctl` said `active` and SSH worked, but the last log line was 12 hours
old, no data file had changed since Friday 21:54Z and the dashboard did not
answer. Scans had fallen from up to 30 an hour to 0-3 from Friday 08:00Z, during
market hours. `yfinance` logged `SPY: possibly delisted` and the like, which is
false: most likely failed requests during the stall.

### Diagnosis

- The app was at `MemoryHigh` (~430M) with swap at `MemorySwapMax` (100M), so it
  had nowhere to go, and spent ~80% of its time stalled on memory (the cgroup's
  `memory.pressure`). In a 30s sample: 34.5s kernel, 0.23s user. The stack was
  `mem_cgroup_handle_over_high` -> `try_to_free_mem_cgroup_pages`.
- **That state is stable.** `MemoryHigh` throttles the process so hard that it
  never reaches `MemoryMax`: `oom_kill` stayed 0. The previous run sat like this
  for ~29 hours until `RuntimeMaxSec`, and even then the SIGTERM timed out and
  it was SIGKILLed, so the save on shutdown probably did not finish.
- Each of the last two runs got there within 18-30 hours of a restart, while
  the Oct 24-26 run stayed flat for two days. Why it got faster after the
  Oct 26-27 deploy is not established.
- **The leak:** 7,779 mappings of ~8MB with guard pages, 62GB virtual, and only
  8 live threads. They held 85MB of RAM, 97 of the 100MB of swap, and ~33MB of
  page tables. `yf.download(threads=True)` starts threads through the
  `multitasking` library, which keeps each one in `config["TASKS"]`, and yfinance
  never joins them, so a finished thread's stack stays mapped. Reproduced in the
  app's venv (Python 3.13.5): 200 finished tasks left 200 stacks; joining them
  and clearing the list left 4. A real 25-ticker download left 25 retained tasks
  with `threads=True` and 0 with `threads=False`.

### Changes

- `price_lookup.py`: `threads=False` on both `yf.download` calls.
- `server.py` / `main.py`: a watchdog thread pings systemd while the scan loop
  makes progress (`loop_stalled`, 30 minutes without a beat stops the pings).
- Unit (`DEPLOY.md` section 6): `WatchdogSec=5min`, `WatchdogSignal=SIGTERM`,
  `MemoryHigh` removed, `MemoryMax=500M`.
- VM: journal capped at 200M. The cap the September entry below lists was not in
  place, and the journal had reached 2.8GB.

**Lesson:** `active` means little here. Check that `Memory in use` is still being
logged, and the count of ~8MB anonymous regions in `/proc/<pid>/maps`.

---

## 2026-10-24: memory full again - the October unit fixes were never applied

### What it looked like

SSH timed out and Tailscale had shown the VM offline for 3 days. The serial
log showed the app still scanning, but DNS and the metadata server were
unreachable (`dial tcp 169.254.169.254:80: connect: network is
unreachable`), and `google-guest-agent-manager` was at restart counter
2,628, logging `Found left-over process ... (core_plugin)` on every start.

### Diagnosis

- `systemctl show stocks-ai` on the VM: `RuntimeMaxUSec=infinity`,
  `MemorySwapMax=infinity`. The unit was still the September version. The
  `RuntimeMaxSec=2d` / `MemorySwapMax=100M` / `PYTHONUNBUFFERED=1` lines
  from the incident below were committed to `DEPLOY.md` but never applied.
  The app ran 11 days (Oct 12 - 23) without a restart and grew into zram.
- journald `Under memory pressure`: 1-21 a day from Oct 12 to Oct 19, then
  ~5,000 a day from Oct 20. That is the same livelock as September.
- The guest agent's crash loop (100ms `RestartSec`, `KillMode=process`)
  orphaned its plugins on each of the 2,628 restarts. It started only ~7h
  before the stop, so it made things worse but did not start them.

### Changes

- VM: `stocks-ai.service` brought up to date with `DEPLOY.md` section 6.
- VM: a drop-in for `google-guest-agent-manager` (`KillMode=control-group`,
  `RestartSec=30s`, `MemoryMax=200M`), now in `DEPLOY.md`.
- Recovered with `stop` / `start`.

**Lesson:** after changing the unit in `DEPLOY.md`, check
`systemctl show stocks-ai -p RuntimeMaxUSec -p MemorySwapMax` on the VM.
A `git pull` does not update `/etc/systemd/system`.

---

## 2026-10: "RAM full at weekends" - OOM kill after ~4.5 days of uptime

### What it looked like

The VM slowed to a crawl from a Friday through the weekend, two weeks
running. The hourly snapshots in the local `VM files` repo (normally taken
at :00-:05) started arriving at :08-:52 past the hour, with a 9-hour gap.

| Process | Started | Snapshots late from | Uptime then | End |
|---|---|---|---|---|
| 919486 | Wed Sep 30 ~14:00Z | Fri Oct 3 ~07:00Z | ~2.7 d | OOM kill Sun Oct 5 02:12Z |
| 1112814 | Sun Oct 5 02:13Z | Fri Oct 9 ~17:00Z | ~4.6 d | still thrashing Sat Oct 10 |

```
stocks-ai.service: A process of this unit has been killed by the OOM killer.
stocks-ai.service: Consumed 1h 34min 41.461s CPU time, 373.9M memory peak, 405.7M memory swap peak.
```

### Diagnosis

- **Not weekend-specific.** Both slowdowns began on a Friday, before the
  weekend scan schedule (`is_weekend`, Saturday 04:00Z) applies. The process
  grows slowly with uptime, roughly 4-7 MB an hour. The slowdown starts when
  it crosses `MemoryHigh=420M` and the kernel pushes it into zram. With
  restarts early in the week, that lands on Friday or the weekend.
- `MemoryMax=550M` caps RAM only. The process went on into swap (374M + 406M)
  until the whole VM ran out, so the kill was a global OOM after ~19 hours of
  thrashing, not the unit's own cap.
- Ruled out: open positions (fell all week, 7-13 tickers), paper marks
  (~300), dashboard polling (cheap endpoints only), and every collection in
  the scan loop (all capped; the Python heap stayed flat in a forced-weekend
  local run).
- The `period=1y` yfinance errors that start in the same hours come from the
  `fast_info` price fallback after Yahoo requests began failing. They look
  like a symptom of the thrashing, not the cause.
- Leak source not found at the time (found 2026-10-31, see the entry above).
  Possibly part of it: 120 repeated
  `price_lookup.fetch_prices` calls grew process memory ~13 MB on Windows
  (~0.1 MB a call, ~1 MB/h at the watch-check rate). Unconfirmed on the VM.
- Log timestamps from before the fix are flush times, not event times:
  Python's stdout was block-buffered under systemd.

### Changes

- systemd unit (`DEPLOY.md` section 6): `RuntimeMaxSec=2d` (planned restart
  well before `MemoryHigh`), `MemorySwapMax=100M` (the cgroup kills the app
  instead of the VM thrashing), `Environment=PYTHONUNBUFFERED=1`.
- `server.py`: a SIGTERM handler runs `backend.stop()`, so a restart saves
  the processed URLs and counters instead of re-alerting recent articles.
- `main.py`: the per-scan `Memory in use` reading also goes to stdout, i.e.
  journald, where it survives a restart.

### Open follow-up

Find the leak: plot `journalctl -u stocks-ai | grep "Memory in use"` over a
couple of days and line the jumps up with watch checks, Reddit passes and
AI calls.

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
