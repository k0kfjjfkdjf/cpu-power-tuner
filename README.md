# cpu-power-tuner

**English** · [Русский](README.ru.md)

**A "potato mode" for your laptop: longer on battery, quieter, and no lag when one core is
working hard.** It nudges the Linux scheduler to place heavy tasks with core temperatures in mind —
something the scheduler itself does not do on most x86 laptops.

A CoreCtrl-style power manager for Linux laptops and desktops. It shows every core and thread
at a glance (load, frequency, temperature, package power) and applies **presets of CPU limits**
that follow your power profile: power-saver / balanced / performance from `power-profiles-daemon`,
or a profile you pick by hand when that daemon is absent.

Beyond plain limits it can react to heat per core: **smart throttling** of a single hot core,
**task parking** (moving the heaviest task off a warming core to a cooler one) and pinning the
**focused window** to the coolest cores so they can boost, while the remaining cores rest at a lower
ceiling ("focus mode"); the two groups swap roles as they heat up.

![Main window](docs/monitor.png)

> The GUI is currently in Russian. Every control is described below in English.

---

## Contents

- [Features](#features)
- [Compatibility](#compatibility)
- [Install, update, remove](#install-update-remove)
- [Quick start](#quick-start)
- [How it works](#how-it-works)
- [Static preset parameters](#static-preset-parameters)
- [Smart throttling and the grace period](#smart-throttling-and-the-grace-period)
- [Task parking](#task-parking)
- [Focus mode: focused window on cool cores](#focus-mode-focused-window-on-cool-cores)
- [Dynamic presets (battery steps)](#dynamic-presets-battery-steps)
- [Profiles and power-profiles-daemon](#profiles-and-power-profiles-daemon)
- [Configuration file](#configuration-file)
- [Command line](#command-line)
- [Safety and undo](#safety-and-undo)
- [Test tools](#test-tools)
- [Measured on a real laptop](#measured-on-a-real-laptop)
- [Troubleshooting](#troubleshooting)
- [Project layout](#project-layout)

---

## Features

- **Monitor** — package temperature and power, average/peak frequency, load, iGPU frequency,
  battery; per-core temperatures; per-thread frequency and load. A graph with one lane per metric
  (auto-scaled), or all metrics overlaid; optional smoothing.
- **Static presets** — power limits PL1/PL2, hardware throttle temperature (TCC), turbo on/off,
  frequency ceiling, "boost only while cool", energy-performance preference (EPP), smart throttling,
  task parking, focused-window placement. Every parameter has a "manage" switch: off means
  *leave the factory value*.
- **Dynamic presets** — switch static presets by battery level ("≤ 50 % → Quiet, ≤ 20 % → Eco,
  on AC → something else"), with hysteresis.
- **Built-in presets** — Eco, Quiet, Balanced, Performance, Auto-by-battery. Edit, copy, reset to
  defaults; create and delete your own.
- **Binding** — each power profile (power-saver / balanced / performance) gets any preset.
- **Factory values** are captured on first start and restored when the service stops.
- **Survives firmware and daemon interference** — values are re-checked every few seconds and
  re-applied if something (firmware after resume, `power-profiles-daemon` on AC/battery change)
  overwrote them.

| Static preset | Dynamic preset | System tab |
|---|---|---|
| ![](docs/presets.png) | ![](docs/dynamic.png) | ![](docs/system.png) |

## Compatibility

Everything is detected at runtime. What the machine lacks is hidden in the GUI and never written.

| Feature | Works on | Linux interface |
|---|---|---|
| Power limits PL1/PL2 | Intel (Sandy Bridge and newer) | `powercap/intel-rapl:0` |
| Firmware limit shown | Intel with `processor_thermal` | `powercap/intel-rapl-mmio:0` (read only) |
| Package power reading | Intel, AMD Zen | `intel-rapl:0/energy_uj` |
| Hardware throttle temp (TCC) | Intel (Skylake and newer, if not locked by BIOS) | `intel_tcc_cooling` cooling device |
| Turbo on/off | Intel `intel_pstate`; AMD and others via `cpufreq/boost` | `no_turbo` or `boost` |
| Frequency ceiling | any `cpufreq` driver, incl. ARM; hybrid P/E cores respected | `scaling_max_freq` per thread |
| EPP | `intel_pstate`, `amd-pstate` (active mode) | `energy_performance_preference` |
| Per-core temperatures | Intel `coretemp` (per core); AMD `k10temp`/`zenpower` (per CCD, approximate) | hwmon |
| Package temperature only | everything else (ARM SoCs etc.) — per-core features are then disabled | thermal zone |
| Multi-socket, hybrid CPUs | cores keyed by `package_id` + `core_id`; per-thread max frequency | topology |
| iGPU frequency (display only) | Intel `i915`, Intel `xe`, AMD `amdgpu` | DRM sysfs |
| Focused window | Hyprland, Sway, i3 | compositor IPC sockets |
| Power profiles | `power-profiles-daemon`; without it a manual profile switch | `state.ini` |
| Distros (installer) | Arch, Debian/Ubuntu, Fedora, openSUSE; others manually | — |

Requirements: Linux with systemd, Python ≥ 3.11, PyGObject + pycairo, GTK 4.

**Tested on hardware:** HP 15s-fq1xxx, Intel Core i5-1035G1 (Ice Lake), Arch Linux, kernel 7.2, Hyprland.
AMD, ARM and Sway paths are tested against simulated sysfs/IPC only — reports welcome.

## Install, update, remove

```bash
git clone https://github.com/k0kfjjfkdjf/cpu-power-tuner.git
cd cpu-power-tuner
sudo ./install.sh        # same command updates an existing install
```

The installer:
- installs dependencies with `pacman` / `apt` / `dnf` / `zypper`;
- offers to disable services that fight over the same knobs (TLP, auto-cpufreq, thermald);
- copies the program to `/opt/cpu-power-tuner`, adds `cpu-power-tuner` (GUI) and `cpt` (CLI) to
  `/usr/local/bin` and a desktop entry;
- enables and (re)starts `cpu-power-tuner.service`.

Only members of `wheel` (or `sudo`/`admin`, whichever exists) can control the service from the GUI.
Add yourself with `sudo usermod -aG wheel $USER` and log in again.

Remove: `sudo ./uninstall.sh` (keeps config) or `sudo ./uninstall.sh --purge`.

> **First start:** factory values are captured from the *current* settings. If you changed limits
> by hand before installing, reboot first.

## Quick start

1. Open **CPU Power Tuner** from the app menu (or run `cpu-power-tuner`).
2. Top half — metrics. Tick what you want on the graph; lanes vs overlay and smoothing are above the list.
3. **Presets** tab (bottom) — the bar "power-profiles-daemon profile → preset" decides which preset
   runs in each profile. The running preset has an orange frame and a green "running" badge.
4. Click **Configure** on a card, change values, **Save and apply**. Changes take effect immediately.
5. Switch profiles with the buttons in the window header.

## How it works

```
 power profile ──► bound preset ──► (dynamic?) step by battery ──► static preset
 (ppd or manual)                                                         │
                                                                         ▼
 temperatures ──► smart throttling / boost-by-temp overlay ──► target values ──► sysfs
      │                                                                  ▲
      └──► task parking, focused window ──► CPU affinity (sched_setaffinity)
```

- **`cpt/daemon.py`** — a root service. Every **0.5 s** it checks the power profile, battery,
  temperatures (only when a per-core feature is on) and reacts. Every **5 s** it re-reads all
  managed values and restores anything that was overwritten. It talks to the GUI over a Unix socket
  (`/run/cpu-power-tuner/sock`, newline-delimited JSON).
- **`cpt/hw.py`** — detects hardware and reads/writes sysfs. Writes are skipped when the value is
  already set; if the kernel rounds or clamps a value (RAPL has 0.125 W steps), the rounded result
  is remembered so it is not rewritten forever.
- **`cpt/park.py`**, **`cpt/focus.py`** — CPU-affinity features.
- **`cpt/gui.py`** — GTK 4 interface, runs as your user and never touches hardware directly.
- **`cpt/config.py`** — presets, validation, TOML read/write.

## Static preset parameters

| Parameter | What it does | Linux knob | When switched off |
|---|---|---|---|
| **PL1**, W | Long-term package power limit (≈28 s average). The main lever for silence and battery life. | `intel-rapl:0/constraint_0_power_limit_uw` | factory value |
| **PL2**, W | Short-term (burst) limit. Equal to PL1 → no heat spikes at all. | `constraint_1_power_limit_uw` | factory value |
| **Hardware throttle temp**, °C | The CPU itself throttles above this temperature (Tjmax − TCC offset). Coarse — use as a safety net. | `intel_tcc_cooling` `cur_state` | factory value |
| **Turbo** | Off → never above base frequency. | `intel_pstate/no_turbo` or `cpufreq/boost` | factory value |
| **Boost ceiling**, MHz | Max frequency of all threads (each capped at its own hardware max on hybrid CPUs). | `scaling_max_freq` | hardware max |
| **Boost only while package < N °C** | Turbo is switched off above N and back on 5 °C lower. | as Turbo | not limited |
| **EPP** | How eagerly cores raise frequency. `power` = maximum saving. | `energy_performance_preference` | whatever `power-profiles-daemon` sets |
| **Smart throttling** | See below. | `scaling_max_freq` of the hot core's threads | off |
| **Task parking** | See below. | `sched_setaffinity` | off |
| **Focus mode** | See below. | `sched_setaffinity`, `setpriority`, per-core `scaling_max_freq` and EPP | off |

Tip from real measurements: power limits are far more efficient than the TCC threshold. A TCC
threshold of 50 °C produced 231 MHz at 7 W (clock skipping), while a 7 W power limit gave 845 MHz.

## Smart throttling and the grace period

Fields: **trigger °C**, **hold s**, **hot-core frequency MHz**, **package limit W** (0 = none),
**grace s**.

When a core reaches the trigger temperature, the threads of *that core only* get their
`scaling_max_freq` lowered to the chosen frequency for *hold* seconds; other cores keep working.
Optionally the whole package limit (PL1/PL2) is lowered at the same time.

**Grace period** (works when task parking is on): instead of throttling immediately, the core gets
*grace* seconds during which parking urgently evacuates it — one heavy task every 0.5 s, even below
parking's own trigger. If the core cools by 2 °C below the trigger, throttling is cancelled
("cooled without throttling"). Otherwise the frequency is cut as usual. This also filters sensor
noise when a core hovers right at the threshold.

## Task parking

Fields: **trigger °C**, **hold s** (0 = until the new core heats up), **minimum task load %**.

1. Nothing happens while all cores are below the trigger — the process list is not even read.
2. When a core reaches the trigger, a cheap check of `/proc/stat` tells whether that core is busy
   at least *minimum load %*. Only then are all threads scanned (`/proc/*/task/*/stat`), twice,
   0.5 s apart, to measure each thread's load.
3. **Destination core**, in order of preference:
   - *main rule* — below the trigger **and** at least 3 °C cooler than the hot core;
   - *fallback* — the coolest available core that is at least 1 °C cooler ("take what exists").
     Equal temperature is never used (it would ping-pong on sensor noise).

   Destinations exclude cores under smart throttling or in grace, cores given to the focused
   window, and cores whose threads are already more than 50 % busy.
4. The heaviest thread on the hot core is pinned to the destination core's threads. At most one
   task per 2 s (per 0.5 s during a grace period).
5. **Release** — after *hold* seconds; or, with hold = 0, the task stays until its new core reaches
   the trigger, then moves to a cooler core (not more often than every 5 s). If the new core is
   throttled and nothing is cooler, the task is released.

Never parked: kernel threads, PID 1, real-time tasks, tasks whose own affinity excludes the
destination, the focused window's processes, and **process launchers** (compositors, terminals,
shells, launchers, bars — Hyprland, sway, kitty, fish, waybar, …). CPU affinity is inherited by
children: parking a compositor would pin every app launched afterwards. If a parked task does start
children, they are restored together with it.

All pins are journaled to `/var/lib/cpu-power-tuner/pins.json`; after a crash or restart the
service restores exactly what it pinned (checking each thread's start time and the boot id).

## Focus mode: focused window on cool cores

The cores are split into a **working group** (N coolest cores) and a **resting group** (the rest).

| Field | Meaning |
|---|---|
| number of coolest cores | size of the working group (N) |
| re-check every, s | how often the choice is reviewed |
| move if others are cooler by, °C | heat-based rotation threshold |
| rotation by timer, s | swap the groups every T seconds regardless of heat (0 = heat only) |
| window priority (nice) | priority boost for the focused app, e.g. −5 (0 = untouched) |
| compositor priority (nice) | priority boost for Hyprland/Sway/… (0 = untouched) |
| resting cores: ceiling minus, MHz | the resting group runs this much below the preset's ceiling (0 = no) |
| EPP power for resting cores | resting cores raise frequency reluctantly (1 = yes) |

How it works:
1. Once a second the service asks the compositor which window is focused (Hyprland
   `j/activewindow`; Sway/i3 `GET_TREE`) and takes its process with all descendants (browsers and
   Electron apps do their work in child processes).
2. That tree is pinned to the working group and gets a higher priority (`nice`), so when cores are
   scarce the scheduler serves your window first.
3. The resting group gets a lower frequency ceiling and EPP `power`: background tasks living there
   run slower and cooler. Frequency in Linux is per core, not per process, so this is a soft split —
   a background task may still land on a working core, but there it yields to the focused window.
4. **Rotation** — when the working group becomes hotter than the resting one by the margin, when a
   working core is throttled, or every T seconds, the groups swap roles: the window moves, and the
   lower ceiling and EPP move to the new resting group.
5. The compositor is **never pinned** (every app it launches would inherit the pin) — it only gets
   the optional priority boost.
6. When focus moves to another window, the previous application gets its original affinity and
   priority back, including children it started meanwhile.

The working-group size is a ceiling for the focused app; give heavy multi-threaded work more cores.

## Dynamic presets (battery steps)

- **Steps**: "when battery ≤ X % → use static preset P". The step with the lowest matching threshold wins.
- **Above all thresholds** — the preset used on battery when no step matches.
- **On AC** — a separate preset, or "same as above all thresholds".
- **Hysteresis** — when charging back up, a step is kept until the battery is 2 % above its
  threshold (`general.hysteresis_pct`).

## Profiles and power-profiles-daemon

With `power-profiles-daemon` the service follows its current profile (read from
`/var/lib/power-profiles-daemon/state.ini`), and the header buttons call `powerprofilesctl set`.
`power-profiles-daemon` still sets EPP itself; when a preset manages EPP, the service overrides it,
and re-applies it if the daemon changes it again (e.g. on AC/battery switch).

Without `power-profiles-daemon` the header shows "profile (manual)"; the buttons (or
`cpt profile <name>`) switch the profile inside the service, and it is remembered across restarts.

## Configuration file

`/etc/cpu-power-tuner/config.toml` — written by the GUI, may be edited by hand; apply with `cpt apply`.

```toml
[general]
socket_group = "wheel"      # who may control the service (falls back to sudo/admin)
verify_interval = 5         # seconds between re-checks
restore_on_stop = true      # restore factory values when the service stops
hysteresis_pct = 2          # dynamic presets

[bindings]                  # power profile -> preset id
power-saver = "eco"
balanced = "auto_battery"
performance = "performance"

[presets.eco]
name = "Eco"
type = "static"
builtin = "eco"             # built-in presets can be reset to defaults
pl1_w = { enabled = true, value = 7.0 }
pl2_w = { enabled = true, value = 8.0 }
temp_limit_c = { enabled = true, value = 70 }
turbo = { enabled = false, value = true }
max_freq_mhz = { enabled = false, value = 3600 }
boost_temp_c = { enabled = false, value = 85 }
epp = { enabled = true, value = "power" }
smart = { enabled = true, value = { trigger_c = 75, hold_s = 3.0, core_mhz = 800, pkg_w = 0.0, grace_s = 2.0 } }
park = { enabled = true, value = { trigger_c = 70, hold_s = 0.0, min_load_pct = 50 } }
focus = { enabled = false, value = { cores = 2, recheck_s = 5.0, swap_margin_c = 4, rotate_s = 0.0, nice = -5, comp_nice = 0, bg_drop_mhz = 200, bg_epp_power = 1 } }

[presets.auto_battery]
name = "Auto by battery"
type = "dynamic"
on_ac = ""                  # "" = same as default
default = "balanced"
steps = [{ below = 50, preset = "quiet" }, { below = 20, preset = "eco" }]
```

Other files: `/var/lib/cpu-power-tuner/factory.json` (factory values), `pins.json` (affinity journal),
`profile` (manual profile when there is no ppd).

## Command line

| Command | Does |
|---|---|
| `cpt status` | profile → preset → running preset, limits, every thread |
| `cpt apply` | re-read `config.toml` and apply now |
| `cpt config` | print the active configuration |
| `cpt profile <power-saver\|balanced\|performance>` | switch profile (only without power-profiles-daemon) |
| `cpt unpin` | restore CPU affinities left by a previous run; on SMT CPUs also reset any "whole-core" pins the service does not track |
| `journalctl -u cpu-power-tuner -f` | live log: profile changes, parking, throttling |

## Safety and undo

- Only documented kernel interfaces are written (sysfs, powercap, cooling devices, `sched_setaffinity`).
  No MSR writes, no undervolting.
- Every value is validated and clamped (e.g. PL2 ≥ PL1, frequencies within hardware limits).
- Stopping the service (`systemctl stop cpu-power-tuner`, uninstall) restores factory values and
  releases every affinity it set.
- A reboot always returns the hardware to firmware defaults; the service then applies your preset again.

## Test tools

| Tool | Purpose |
|---|---|
| `sudo ./probe/probe.sh` | read-only survey of the machine → `results/probe-<host>-<date>.txt` |
| `sudo ./tests/run_test.sh rapl 7` (and `tcc`, `turbo`, `epp`, `gpu`, `all`) | apply one knob under full load, measure, check it sticks for 30 s, restore |
| `python3 tests/duck.py` | a decoy task `cpt-duck` loading one core; logs where it runs, affinity changes, temperatures and service events → `results/duck-*.log/.tsv` |

Run the duck outside your terminal's process tree when testing parking, otherwise the focused-window
feature owns it: `systemd-run --user --pty python3 "$PWD/tests/duck.py" --load 90 --time 180`.

## Measured on a real laptop

i5-1035G1, all 8 threads at 100 % for 15 s, on AC, ppd = balanced (`results/FINDINGS.md`):

| Setting | Package power | Avg frequency | Max temp |
|---|---|---|---|
| no limits | 23.8 W | 2456 MHz | 96 °C |
| PL1 = PL2 = 7 W | 7.7 W | 845 MHz | 72 °C |
| PL1 = PL2 = 8 W | 8.6 W | 1078 MHz | 71 °C |
| turbo off | 8.2 W | 999 MHz | 71 °C |
| EPP = power | 9.6 W | 1299 MHz | 74 °C |

Parking test with the duck (single run, power limit 5 W): a 90 % task sitting on core 0 at 56 °C was
moved to core 3 at 52 °C and stayed there for the remaining 2.7 minutes. Core 0 stayed at 52–55 °C
(56–58 °C in an earlier run without parking), and smart throttling never had to cut frequency.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "No connection to the service" in the GUI | `systemctl status cpu-power-tuner`; make sure you are in the `wheel`/`sudo` group and re-logged |
| A value I set in the GUI reverts | old GUI window still open after an update — close and reopen it |
| `sudo ./install.sh: command not found` | the file lost its exec bit: `sudo bash ./install.sh` |
| Programs run only on some cores | `cpt unpin`; check with `taskset -p <pid>` (fish: `taskset -p $fish_pid`) |
| Smart throttling / parking missing in the GUI | no per-core temperature sensors on this machine (see the System tab) |
| Focused window: "environment not supported" | only Hyprland, Sway and i3 are supported |
| PL1/PL2 writes fail | limits are locked by BIOS (`rapl` lock bit) — use frequency ceiling, turbo and EPP instead |

## Project layout

| Path | Content |
|---|---|
| `cpt/` | the program (daemon, GUI, CLI, hardware layer) |
| `data/` | systemd unit, desktop entry |
| `install.sh`, `uninstall.sh` | install / update / remove |
| `probe/probe.sh` | hardware survey |
| `tests/run_test.sh`, `tests/duck.py` | manual tests |
| `results/` | survey and test results from the author's laptop, summary in `FINDINGS.md` (Russian) |
| `docs/` | screenshots |

License: not chosen yet — all rights reserved by the author until a license file is added.
