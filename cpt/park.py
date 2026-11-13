"""Умная парковка: пока ядро только подходит к перегреву, самая тяжёлая задача на нём
переезжает на самое холодное свободное ядро (sched_setaffinity), через hold_s отпускается.

Затраты: список задач читается только когда какое-то ядро выше порога, иначе ничего не делаем.
Не трогаем: потоки ядра, PID 1, саму службу, realtime-задачи и задачи, которым пользователь
уже ограничил ядра так, что холодное ядро в них не входит.
"""
import os
import time

PF_KTHREAD = 0x00200000
OK_POLICIES = {0, 3, 5}  # SCHED_OTHER, SCHED_BATCH, SCHED_IDLE
MIN_GAP = 2.0            # не чаще одной парковки за столько секунд
CLK = os.sysconf("SC_CLK_TCK")


def _tasks():
    """{tid: (comm, ticks, cpu)} для всех пользовательских потоков."""
    out = {}
    me = os.getpid()
    try:
        pids = [e.name for e in os.scandir("/proc") if e.name.isdigit()]
    except OSError:
        return out
    for pid in pids:
        if pid == "1" or int(pid) == me:
            continue
        try:
            tids = [e.name for e in os.scandir(f"/proc/{pid}/task")]
        except OSError:
            continue
        for tid in tids:
            try:
                with open(f"/proc/{pid}/task/{tid}/stat") as f:
                    data = f.read()
            except OSError:
                continue
            l, r = data.find("("), data.rfind(")")
            comm = data[l + 1:r]
            rest = data[r + 2:].split()
            try:
                if int(rest[6]) & PF_KTHREAD or int(rest[38]) not in OK_POLICIES:
                    continue
                out[int(tid)] = (comm, int(rest[11]) + int(rest[12]), int(rest[36]))
            except (IndexError, ValueError):
                continue
    return out


class Parker:
    def __init__(self, hw, event):
        self.hw = hw
        self.event = event
        self.prev = None          # (время, снимок задач)
        self.parked = {}          # tid -> dict(orig, set, until, comm, frm, to)
        self.last_park = 0.0

    def status(self):
        now = time.monotonic()
        return [{"tid": t, "comm": p["comm"], "from": p["frm"], "to": p["to"], "left": round(p["until"] - now)}
                for t, p in self.parked.items()]

    def release(self, tid, why=""):
        p = self.parked.pop(tid, None)
        if not p:
            return
        try:
            if os.sched_getaffinity(tid) == p["set"]:  # если кто-то поменял привязку сам — не лезем
                os.sched_setaffinity(tid, p["orig"])
        except OSError:
            pass  # задача уже завершилась
        if why:
            self.event(f"парковка: {p['comm']} отпущен ({why})")

    def release_all(self, why=""):
        for tid in list(self.parked):
            self.release(tid, why)
        self.prev = None

    def tick(self, cfg, core_t, hot_cores):
        """cfg — значение параметра park или None (выключено)."""
        now = time.monotonic()
        for tid in [t for t, p in self.parked.items() if p["until"] <= now]:
            self.release(tid, "время вышло")
        if not cfg:
            if self.parked:
                self.release_all("парковка выключена")
            self.prev = None
            return
        trig = cfg["trigger_c"]
        warm = [c for c, t in core_t.items() if t >= trig]
        if not warm:
            self.prev = None
            return
        snap = _tasks()
        prev, self.prev = self.prev, (now, snap)
        if prev is None or now - self.last_park < MIN_GAP:
            return
        dt = max(now - prev[0], 0.1)
        busy = {}   # tid -> % одного ядра
        for tid, (comm, ticks, cpu) in snap.items():
            old = prev[1].get(tid)
            if old:
                busy[tid] = (ticks - old[1]) / CLK / dt * 100
        cpu_load = {}
        for tid, pct in busy.items():
            cpu_load[snap[tid][2]] = cpu_load.get(snap[tid][2], 0) + pct

        hw = self.hw
        # куда: самое холодное ядро ниже порога (с запасом 3 °C), не под умным троттлингом и не забитое
        dests = []
        for core, t in core_t.items():
            if t >= trig - 3 or core in hot_cores:
                continue
            cpus = hw.threads_of_core(core)
            load = sum(cpu_load.get(c, 0) for c in cpus) / max(len(cpus), 1)
            if load < 50:
                dests.append((t, load, core))
        if not dests:
            return
        dests.sort()
        _t, _l, dest = dests[0]
        dest_cpus = set(hw.threads_of_core(dest))

        for core in sorted(warm, key=lambda c: -core_t[c]):
            src_cpus = set(hw.threads_of_core(core))
            cands = sorted(((pct, tid) for tid, pct in busy.items()
                            if snap[tid][2] in src_cpus and pct >= cfg["min_load_pct"] and tid not in self.parked),
                           reverse=True)
            for pct, tid in cands:
                try:
                    orig = os.sched_getaffinity(tid)
                    new = orig & dest_cpus
                    if not new:
                        continue
                    os.sched_setaffinity(tid, new)
                except OSError:
                    continue
                comm = snap[tid][0]
                self.parked[tid] = {"orig": orig, "set": new, "until": now + cfg["hold_s"],
                                    "comm": comm, "frm": core, "to": dest}
                self.last_park = now
                self.event(f"парковка: {comm} ({pct:.0f}% ядра) ядро {core} ({core_t[core]:.0f} °C) → "
                           f"ядро {dest} ({core_t[dest]:.0f} °C) на {cfg['hold_s']:g} с")
                return
