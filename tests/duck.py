#!/usr/bin/env python3
"""«Утка»: подсадной процесс cpt-duck, который грузит одно ядро, и логгер, который пишет,
как она перемещается по процессору и что с ней делает служба.

Запуск (от обычного пользователя, sudo не нужен):
    python3 tests/duck.py                    # 80 % одного ядра, 180 с
    python3 tests/duck.py --load 60 --time 300
    python3 tests/duck.py --ducks 2          # две утки — греть сильнее

Пишет в results/:
    duck-<host>-<дата>.log  — понятный журнал: переезды, привязки, события службы, сводка
    duck-<host>-<дата>.tsv  — сырые замеры каждые 0.5 с (для графиков)

Чтобы увидеть парковку, нагрузка утки должна быть ≥ min_load_pct парковки в активном пресете,
а ядро под ней должно дойти до порога парковки.
"""
import argparse
import ctypes
import glob
import multiprocessing as mp
import os
import signal
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)


# ---------------------------------------------------------------- утка
def duck(load_pct, idx):
    try:  # имя процесса, чтобы узнавать утку в событиях службы
        ctypes.CDLL(None).prctl(15, f"cpt-duck{idx}".encode(), 0, 0, 0)
    except Exception:  # noqa: BLE001
        pass
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    period = 0.1
    busy = period * load_pct / 100
    x = 0
    while True:
        end = time.perf_counter() + busy
        while time.perf_counter() < end:
            x = (x * 1103515245 + 12345) & 0xFFFFFFFF
        if busy < period:
            time.sleep(period - busy)


# ---------------------------------------------------------------- датчики
def where(pid):
    """(cpu, utime+stime) по /proc/pid/stat."""
    with open(f"/proc/{pid}/stat") as f:
        rest = f.read().rsplit(")", 1)[1].split()
    return int(rest[36]), int(rest[11]) + int(rest[12])


def coretemp():
    for d in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            if open(d + "/name").read().strip() == "coretemp":
                return d
        except OSError:
            pass
    return None


def temps(d):
    cores, pkg = {}, None
    if not d:
        return cores, pkg
    for lab in glob.glob(d + "/temp*_label"):
        try:
            label = open(lab).read().strip()
            t = int(open(lab.replace("_label", "_input")).read()) / 1000
        except (OSError, ValueError):
            continue
        if label.startswith("Package"):
            pkg = t
        elif label.startswith("Core "):
            cores[int(label.split()[1])] = t
    return cores, pkg


def core_of(cpu):
    try:
        return int(open(f"/sys/devices/system/cpu/cpu{cpu}/topology/core_id").read())
    except OSError:
        return cpu


def fmt_set(s):
    return ",".join(map(str, sorted(s)))


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="подсадная утка для проверки парковки")
    ap.add_argument("--load", type=int, default=80, help="нагрузка утки, %% одного ядра (по умолч. 80)")
    ap.add_argument("--time", type=int, default=180, help="сколько секунд (по умолч. 180)")
    ap.add_argument("--ducks", type=int, default=1, help="сколько уток (по умолч. 1)")
    args = ap.parse_args()

    host = (open("/etc/hostname").read().strip() if os.path.exists("/etc/hostname") else os.uname().nodename)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    outdir = os.path.join(ROOT, "results")
    os.makedirs(outdir, exist_ok=True)
    log_path = os.path.join(outdir, f"duck-{host}-{stamp}.log")
    tsv_path = os.path.join(outdir, f"duck-{host}-{stamp}.tsv")
    log = open(log_path, "w")
    tsv = open(tsv_path, "w")

    def say(msg):
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line)
        log.write(line + "\n")
        log.flush()

    # служба (не обязательна: без неё просто не будет её событий)
    client = None
    try:
        from cpt.client import Client
        client = Client()
        st = client.request(cmd="status")["status"]
        say(f"служба: профиль ppd {st.get('profile')}, работает пресет {st.get('static_preset')} ({st.get('reason')})")
        cfg = client.request(cmd="get_config")["config"]
        p = cfg["presets"].get(st.get("static_preset"), {})
        for key in ("park", "smart", "focus"):
            it = p.get(key, {})
            say(f"  {key}: {'вкл' if it.get('enabled') else 'выкл'} {it.get('value')}")
        pk = p.get("park", {})
        if pk.get("enabled") and args.load < pk["value"]["min_load_pct"]:
            say(f"  ВНИМАНИЕ: нагрузка утки {args.load}% < min_load_pct {pk['value']['min_load_pct']}% — парковка её не тронет")
    except Exception as e:  # noqa: BLE001
        say(f"служба недоступна ({e}) — пишу только перемещения и температуры")
        client = None

    ct = coretemp()
    ncpu = os.cpu_count()
    cores = sorted({core_of(c) for c in range(ncpu)})
    tsv.write("t\tduck\tcpu\tcore\tload_pct\taffinity\t" + "\t".join(f"core{c}_C" for c in cores) + "\tpkg_C\n")

    procs = []
    for i in range(args.ducks):
        pr = mp.Process(target=duck, args=(args.load, i), daemon=True)
        pr.start()
        procs.append(pr)
    say(f"запущено уток: {args.ducks}, нагрузка {args.load}%, {args.time} с; PID: {', '.join(str(p.pid) for p in procs)}")
    say(f"журнал: {log_path}")

    state = {p.pid: {"cpu": None, "aff": None, "ticks": None, "t": None,
                     "time_on": {}, "sched_moves": 0, "svc_moves": 0, "smt_flips": 0} for p in procs}
    seen_events = set()
    if client:
        try:
            seen_events = set(client.request(cmd="status")["status"].get("events") or [])
        except Exception:  # noqa: BLE001
            pass
    clk = os.sysconf("SC_CLK_TCK")
    t0 = time.monotonic()
    last_report = t0
    stop = False

    def on_int(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGINT, on_int)

    try:
        while not stop and time.monotonic() - t0 < args.time:
            time.sleep(0.5)
            now = time.monotonic()
            ct_cores, pkg = temps(ct)
            for i, pr in enumerate(procs):
                s = state[pr.pid]
                try:
                    cpu, ticks = where(pr.pid)
                    aff = os.sched_getaffinity(pr.pid)
                except OSError:
                    continue
                load = None
                if s["ticks"] is not None:
                    load = (ticks - s["ticks"]) / clk / (now - s["t"]) * 100
                core = core_of(cpu)
                if s["aff"] is not None and aff != s["aff"]:
                    s["svc_moves"] += 1
                    say(f"утка{i}: ПРИВЯЗКА изменена {fmt_set(s['aff'])} → {fmt_set(aff)} "
                        f"(служба), сейчас CPU {cpu} / ядро {core} {ct_cores.get(core, '?')} °C")
                elif s["cpu"] is not None and cpu != s["cpu"]:
                    if core_of(s["cpu"]) == core:
                        s["smt_flips"] += 1   # между двумя потоками одного ядра — не переезд, не пишем
                    else:
                        s["sched_moves"] += 1
                        say(f"утка{i}: переезд CPU {s['cpu']} → {cpu} (ядро {core_of(s['cpu'])} → {core}, планировщик)")
                elif s["aff"] is None:
                    full = len(aff) == ncpu
                    say(f"утка{i}: старт на CPU {cpu} / ядро {core}, привязка {fmt_set(aff)}"
                        + ("" if full else "  ← УЖЕ ограничена (унаследована от родителя?)"))
                s.update(cpu=cpu, aff=aff, ticks=ticks, t=now)
                s["time_on"][core] = s["time_on"].get(core, 0) + 0.5
                tsv.write(f"{now - t0:.1f}\t{i}\t{cpu}\t{core}\t{'' if load is None else f'{load:.0f}'}\t{fmt_set(aff)}\t"
                          + "\t".join(f"{ct_cores.get(c, '')}" for c in cores) + f"\t{pkg if pkg is not None else ''}\n")
            tsv.flush()
            if client:
                try:
                    st = client.request(cmd="status")["status"]
                    for ev in st.get("events") or []:
                        if ev not in seen_events:
                            seen_events.add(ev)
                            say(f"  [служба] {ev[9:]}")
                except Exception:  # noqa: BLE001
                    pass
            if now - last_report >= 10:
                last_report = now
                tl = " ".join(f"я{c}={ct_cores.get(c, '?')}" for c in cores)
                say(f"t={now - t0:.0f} с · температуры: {tl} · пакет {pkg} °C · утки на ядрах: "
                    + ", ".join(str(core_of(state[p.pid]['cpu'])) if state[p.pid]['cpu'] is not None else '?' for p in procs))
    finally:
        for pr in procs:
            pr.terminate()
        say("--- сводка")
        for i, pr in enumerate(procs):
            s = state[pr.pid]
            total = sum(s["time_on"].values()) or 1
            share = ", ".join(f"ядро {c}: {v / total * 100:.0f}%" for c, v in sorted(s["time_on"].items()))
            say(f"утка{i}: время по ядрам — {share}")
            say(f"утка{i}: переездов между ядрами (планировщик) {s['sched_moves']}, смен привязки службой {s['svc_moves']}, "
                f"переключений между потоками одного ядра {s['smt_flips']}")
        say(f"данные: {tsv_path}")
        log.close()
        tsv.close()


if __name__ == "__main__":
    main()
