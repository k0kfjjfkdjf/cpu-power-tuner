"""Умная парковка: пока ядро только подходит к перегреву, самая тяжёлая задача на нём
переезжает на самое холодное свободное ядро (sched_setaffinity), через hold_s отпускается.

Затраты: список задач читается только когда какое-то ядро выше порога, иначе ничего не делаем.
Не трогаем: потоки ядра, PID 1, саму службу, realtime-задачи и задачи, которым пользователь
уже ограничил ядра так, что холодное ядро в них не входит.
"""
import json
import os
import time

PF_KTHREAD = 0x00200000
# Процессы, которые запускают другие программы. Привязка к ядрам наследуется детьми, поэтому
# если припарковать, например, Hyprland, всё запущенное после этого окажется на одном ядре.
SPAWNERS = {
    "Hyprland", "hyprland", "sway", "river", "niri", "labwc", "wayfire", "kwin_wayland", "kwin_x11",
    "gnome-shell", "mutter", "Xwayland", "Xorg", "X", "systemd", "init", "sddm", "gdm", "greetd",
    "login", "sshd", "dbus-daemon", "dbus-broker", "uwsm", "kitty", "alacritty", "foot", "wezterm",
    "wezterm-gui", "konsole", "gnome-terminal-", "xterm", "ghostty", "fish", "bash", "zsh", "sh", "dash",
    "nu", "tmux: server", "tmux", "screen", "rofi", "wofi", "fuzzel", "walker", "anyrun", "waybar",
    "ags", "gjs", "astal", "nwg-drawer",
}
OK_POLICIES = {0, 3, 5}  # SCHED_OTHER, SCHED_BATCH, SCHED_IDLE


def _uptime_ticks():
    with open("/proc/uptime") as f:
        return float(f.read().split()[0]) * CLK


def _all_user_tasks():
    """Итератор (tid, starttime) по всем пользовательским потокам системы."""
    for e in os.scandir("/proc"):
        if not e.name.isdigit():
            continue
        try:
            tids = os.listdir(f"/proc/{e.name}/task")
        except OSError:
            continue
        for tid in tids:
            try:
                with open(f"/proc/{e.name}/task/{tid}/stat") as f:
                    rest = f.read().rsplit(")", 1)[1].split()
                if int(rest[6]) & PF_KTHREAD:
                    continue
                yield int(tid), int(rest[19])
            except (OSError, ValueError, IndexError):
                continue


def restore_inherited(since, pinned, orig):
    """Вернуть привязку детям, которые родились от припаркованной задачи и унаследовали её ядра."""
    n = 0
    for tid, start in _all_user_tasks():
        if start < since:
            continue
        try:
            if os.sched_getaffinity(tid) == pinned:
                os.sched_setaffinity(tid, orig)
                n += 1
        except OSError:
            pass
    return n


def _start_of(tid):
    try:
        with open(f"/proc/{tid}/stat") as f:
            return int(f.read().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def _boot_id():
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            return f.read().strip()
    except OSError:
        return ""


# ---------- журнал привязок: что именно поставила служба (переживает сбой и перезапуск) ----------
def save_pins(path, entries):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"boot_id": _boot_id(), "pins": entries}, f)
    os.replace(tmp, path)


def restore_saved_pins(path):
    """После сбоя/перезапуска: вернуть привязки, которые служба ставила в прошлый раз.
    Трогаем поток, только если он тот же самый (совпадает время старта) и привязка всё ещё наша."""
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return 0
    n = 0
    if data.get("boot_id") == _boot_id():
        for e in data.get("pins", []):
            pinned, orig = set(e["set"]), set(e["orig"])
            if e.get("tid") and _start_of(e["tid"]) == e.get("start"):
                tids = os.listdir(f"/proc/{e['tid']}/task") if e.get("whole_process") else [e["tid"]]
                for t in tids:
                    try:
                        if os.sched_getaffinity(int(t)) == pinned:
                            os.sched_setaffinity(int(t), orig)
                            n += 1
                    except OSError:
                        pass
            if e.get("since") is not None:
                n += restore_inherited(e["since"], pinned, orig)
    try:
        os.remove(path)
    except OSError:
        pass
    return n


def unpin_orphans(hw, keep=()):
    """Глубокая уборка вручную (cpt unpin): сбросить привязки «ровно к целым ядрам», которые служба сейчас
    не ведёт. Только на процессорах с SMT: там такая привязка почти наверняка наша. Без SMT (у ядра один
    поток) под этот признак подходит любая ручная привязка taskset, поэтому там ничего не делаем."""
    cores = {}
    for c in hw.cpus:
        cores.setdefault(hw.core_of.get(c, c), set()).add(c)
    if not cores or min(len(v) for v in cores.values()) < 2:
        return 0
    full = set(hw.cpus)
    keep = set(keep)
    n = 0
    for tid, _start in _all_user_tasks():
        if tid in keep:
            continue
        try:
            aff = os.sched_getaffinity(tid)
        except OSError:
            continue
        if not aff < full:
            continue
        if all(cs <= aff or not (cs & aff) for cs in cores.values()):
            try:
                os.sched_setaffinity(tid, full)
                n += 1
            except OSError:
                pass
    return n


MIN_GAP = 2.0            # не чаще одной парковки за столько секунд
MARGIN = 3.0             # желательно: приёмник ниже порога и на столько °C холоднее источника
RELOCATE_GAP = 5.0       # одну и ту же задачу перевозим не чаще раза в 5 с (датчик шумит на ±1 °C)
MIN_DIFF = 1.0           # резерв: самое холодное ядро, хоть на градус холоднее (на равное не возим — пинг-понг)
CLK = os.sysconf("SC_CLK_TCK")


def _cpu_ticks():
    """{cpu: (всего, простой)} из /proc/stat — дёшево, одно чтение."""
    out = {}
    try:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("cpu") and line[3].isdigit():
                    p = line.split()
                    v = list(map(int, p[1:9]))
                    out[int(p[0][3:])] = (sum(v), v[3] + v[4])
    except OSError:
        pass
    return out


def _tasks():
    """{tid: (comm, ticks, cpu, pid)} для всех пользовательских потоков."""
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
                out[int(tid)] = (comm, int(rest[11]) + int(rest[12]), int(rest[36]), int(pid))
            except (IndexError, ValueError):
                continue
    return out




class Parker:
    def __init__(self, hw, event):
        self.hw = hw
        self.event = event
        self.prev = None          # (время, снимок задач)
        self.prev_cpu = None      # снимок /proc/stat
        self.scans = 0            # сколько раз читали список задач (для отладки)
        self.parked = {}          # tid -> dict(orig, set, until|None, comm, frm, to)
        self.last_park = 0.0

    def pin_entries(self):
        return [{"tid": t, "start": _start_of(t), "set": sorted(p["set"]), "orig": sorted(p["orig"]),
                 "since": p["since"]} for t, p in self.parked.items()]

    def status(self):
        now = time.monotonic()
        return [{"tid": t, "comm": p["comm"], "from": p["frm"], "to": p["to"],
                 "left": None if p["until"] is None else round(p["until"] - now)}
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
        kids = restore_inherited(p["since"], p["set"], p["orig"])
        if why:
            self.event(f"парковка: {p['comm']} отпущен ({why})" + (f", дочерних потоков возвращено: {kids}" if kids else ""))

    def release_all(self, why=""):
        for tid in list(self.parked):
            self.release(tid, why)
        self.prev = self.prev_cpu = None

    def _move(self, tid, core):
        """Перевезти уже припаркованную задачу на другое ядро."""
        p = self.parked[tid]
        new = p["orig"] & set(self.hw.threads_of_core(core))
        if not new:
            return False
        try:
            if os.sched_getaffinity(tid) != p["set"]:
                self.parked.pop(tid)      # привязку поменяли без нас — забываем задачу
                return True
            os.sched_setaffinity(tid, new)
        except OSError:
            self.parked.pop(tid)
            return True
        restore_inherited(p["since"], p["set"], p["orig"])  # дети, родившиеся на старом ядре
        p["set"], p["to"], p["since"], p["moved"] = new, core, _uptime_ticks(), time.monotonic()
        return True

    def tick(self, cfg, core_t, hot_cores, skip_pids=(), skip_cores=(), evac=()):
        """cfg — значение параметра park или None (выключено).
        skip_pids — процессы активного окна (их не трогаем), skip_cores — ядра, отданные активному окну,
        evac — ядра в отсрочке умного троттлинга: их разгружаем в первую очередь и без паузы."""
        now = time.monotonic()
        for tid in [t for t, p in self.parked.items() if p["until"] is not None and p["until"] <= now]:
            self.release(tid, "время вышло")
        for tid in [t for t in self.parked if not os.path.exists(f"/proc/{t}")]:
            p = self.parked.pop(tid)      # задача завершилась — но её дети могли унаследовать привязку
            restore_inherited(p["since"], p["set"], p["orig"])
        if not cfg:
            if self.parked:
                self.release_all("парковка выключена")
            self.prev = self.prev_cpu = None
            return
        trig = cfg["trigger_c"]
        evac = set(evac)

        def candidates():
            """Все ядра, куда в принципе можно везти, от холодного к тёплому."""
            return sorted((c for c in core_t if c not in hot_cores and c not in skip_cores and c not in evac),
                          key=lambda c: core_t[c])

        def pick(src_t, exclude=(), ok=lambda c: True):
            """(ядро, резерв?) — сначала по правилу (ниже порога и на MARGIN холоднее),
            иначе самое холодное из тех, что хоть на MIN_DIFF холоднее источника."""
            cands = [c for c in candidates() if c not in exclude and ok(c)]
            for c in cands:
                if core_t[c] < trig and core_t[c] <= src_t - MARGIN:
                    return c, False
            for c in cands:
                if core_t[c] <= src_t - MIN_DIFF:
                    return c, True
            return None, False

        # бессрочно припаркованные: новое ядро нагрелось -> везём на более холодное, если такое есть
        for tid, p in list(self.parked.items()):
            if p["until"] is not None:
                continue
            to_t = core_t.get(p["to"], 0)
            throttled = p["to"] in evac or p["to"] in hot_cores
            if (to_t < trig and not throttled) or now - p.get("moved", 0) < RELOCATE_GAP:
                continue
            dest, _fb = pick(to_t, exclude=(p["to"],))
            old = p["to"]
            if dest is not None:
                if self._move(tid, dest):
                    self.event(f"парковка: {p['comm']} ядро {old} ({to_t:.0f} °C) нагрелось → ядро {dest} ({core_t[dest]:.0f} °C)")
            elif throttled:
                self.release(tid, f"ядро {old} под троттлингом, холоднее нет")
            # иначе остаётся где есть: это всё ещё самое холодное из возможных

        warm = sorted({c for c, t in core_t.items() if t >= trig} | evac,
                      key=lambda c: (c not in evac, -core_t.get(c, 0)))
        if not warm or pick(core_t.get(warm[0], 0))[0] is None:
            # некуда переносить (или незачем) — список задач даже не читаем, чтобы не тратить CPU
            self.prev = self.prev_cpu = None
            return
        if not evac and now - self.last_park < MIN_GAP:
            self.prev = None
            return
        # дешёвая проверка: есть ли на тёплом ядре вообще тяжёлая работа (по /proc/stat)
        cpu_now, cpu_prev = _cpu_ticks(), self.prev_cpu
        self.prev_cpu = cpu_now
        if cpu_prev is None:
            return
        busy_cpu = {}
        for c, (tot, idle) in cpu_now.items():
            if c in cpu_prev:
                dt_tot = tot - cpu_prev[c][0]
                busy_cpu[c] = 100 * (dt_tot - (idle - cpu_prev[c][1])) / dt_tot if dt_tot > 0 else 0
        if not any(busy_cpu.get(c, 0) >= cfg["min_load_pct"]
                   for core in warm for c in self.hw.threads_of_core(core)):
            self.prev = None
            return
        # только теперь читаем список задач (дорого: все потоки системы)
        snap = _tasks()
        self.scans += 1
        prev, self.prev = self.prev, (now, snap)
        if prev is None:
            return
        dt = max(now - prev[0], 0.1)
        busy = {}   # tid -> % одного ядра
        for tid, (comm, ticks, cpu, _pid) in snap.items():
            old = prev[1].get(tid)
            if old:
                busy[tid] = (ticks - old[1]) / CLK / dt * 100
        cpu_load = {}
        for tid, pct in busy.items():
            cpu_load[snap[tid][2]] = cpu_load.get(snap[tid][2], 0) + pct

        hw = self.hw

        def not_busy(c):  # приёмник не должен быть забит: средняя загрузка его потоков < 50 %
            cpus = hw.threads_of_core(c)
            return sum(cpu_load.get(x, 0) for x in cpus) / max(len(cpus), 1) < 50

        hold = cfg["hold_s"]

        for core in warm:
            dest, fallback = pick(core_t.get(core, 0), exclude=(core,), ok=not_busy)
            if dest is None:
                continue
            dest_cpus = set(hw.threads_of_core(dest))
            src_cpus = set(hw.threads_of_core(core))
            cands = sorted(((pct, tid) for tid, pct in busy.items()
                            if snap[tid][2] in src_cpus and pct >= cfg["min_load_pct"] and tid not in self.parked
                            and snap[tid][3] not in skip_pids
                            and snap.get(snap[tid][3], (snap[tid][0],))[0] not in SPAWNERS
                            and snap[tid][0] not in SPAWNERS),
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
                self.parked[tid] = {"orig": orig, "set": new, "until": now + hold if hold > 0 else None,
                                    "since": _uptime_ticks(), "moved": now,
                                    "comm": comm, "frm": core, "to": dest}
                self.last_park = now
                why = (" (остужаю перед троттлингом)" if core in evac else "") + \
                      (" — резерв: холоднее на 3 °C ядер нет, беру самое холодное" if fallback else "")
                self.event(f"парковка: {comm} ({pct:.0f}% ядра) ядро {core} ({core_t.get(core, 0):.0f} °C) → "
                           f"ядро {dest} ({core_t[dest]:.0f} °C) " + (f"на {hold:g} с" if hold > 0 else "пока не нагреется") + why)
                return
