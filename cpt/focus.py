"""Активное окно — на самые холодные ядра.

Раз в секунду служба спрашивает у Hyprland, какое окно в фокусе, берёт его процесс
вместе со всеми дочерними (у браузеров и Electron-приложений работа идёт в дочерних
процессах) и привязывает их к N самым холодным ядрам, которые не под умным троттлингом.
Раз в recheck_s секунд выбор ядер пересматривается: если выбранные ядра нагрелись,
а другие холоднее на swap_margin_c °C, программа переезжает.
При смене окна прошлое приложение отпускается (возвращается исходная привязка).

Поддерживаются Hyprland и Sway/i3 (через их IPC-сокеты). На других окружениях функция молчит.
"""
import glob
import json
import os
import socket
import struct
import time

from .park import _start_of, _uptime_ticks, restore_inherited

HYPR_GLOB = os.environ.get("CPT_HYPR_GLOB", "/run/user/*/hypr/*/.socket.sock")
SWAY_GLOBS = os.environ.get("CPT_SWAY_GLOB", "/run/user/*/sway-ipc.*.sock:/run/user/*/i3/ipc-socket.*").split(":")


def _sock_list(pattern):
    return sorted(glob.glob(pattern), key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0, reverse=True)


def _focused(node):
    if node.get("focused"):
        return node
    for key in ("nodes", "floating_nodes"):
        for ch in node.get(key) or []:
            f = _focused(ch)
            if f:
                return f
    return None


def sway_active_pid():
    """Sway/i3 IPC: GET_TREE (тип 4), ищем узел с focused=true."""
    socks = [p for g in SWAY_GLOBS for p in _sock_list(g)]
    if not socks:
        return None, None
    for path in socks:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                s.connect(path)
                s.sendall(b"i3-ipc" + struct.pack("<II", 0, 4))
                head = b""
                while len(head) < 14:
                    chunk = s.recv(14 - len(head))
                    if not chunk:
                        raise OSError("обрыв")
                    head += chunk
                size = struct.unpack("<I", head[6:10])[0]
                body = b""
                while len(body) < size:
                    chunk = s.recv(size - len(body))
                    if not chunk:
                        raise OSError("обрыв")
                    body += chunk
            node = _focused(json.loads(body))
            if node and isinstance(node.get("pid"), int) and node["pid"] > 1:
                return node["pid"], node.get("app_id") or (node.get("window_properties") or {}).get("class") or "?"
            return None, "нет активного окна"
        except (OSError, ValueError):
            continue
    return None, "сокет Sway/i3 не отвечает"


def active_pid():
    pid, info = hypr_active_pid()
    if pid is not None or info not in ("Hyprland не найден",):
        return pid, info
    pid, info = sway_active_pid()
    if info is None:
        return None, "окружение не поддерживается (нужен Hyprland или Sway/i3)"
    return pid, info


def hypr_active_pid():
    """(pid, class) активного окна Hyprland или (None, причина)."""
    socks = _sock_list(HYPR_GLOB)
    if not socks:
        return None, "Hyprland не найден"
    for path in socks:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                s.connect(path)
                s.sendall(b"j/activewindow")
                buf = b""
                while True:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
            data = json.loads(buf or b"{}")
            pid = data.get("pid")
            if isinstance(pid, int) and pid > 1:
                return pid, data.get("class") or data.get("title") or "?"
            return None, "нет активного окна"
        except (OSError, ValueError):
            continue  # старый сокет от прошлой сессии
    return None, "сокет Hyprland не отвечает"


def _children_map():
    kids = {}
    for e in os.scandir("/proc"):
        if not e.name.isdigit():
            continue
        try:
            with open(f"/proc/{e.name}/stat") as f:
                data = f.read()
            ppid = int(data[data.rfind(")") + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        kids.setdefault(ppid, []).append(int(e.name))
    return kids


def process_tree(pid):
    kids = _children_map()
    out, stack = [], [pid]
    while stack:
        p = stack.pop()
        out.append(p)
        stack.extend(kids.get(p, []))
    return out


def _tids(pid):
    try:
        return [int(t) for t in os.listdir(f"/proc/{pid}/task")]
    except OSError:
        return []


def _comm(pid):
    try:
        with open(f"/proc/{pid}/comm") as f:
            return f.read().strip()
    except OSError:
        return str(pid)


class Focus:
    def __init__(self, hw, event):
        self.hw = hw
        self.event = event
        self.pid = None
        self.name = ""
        self.cores = []
        self.pinned = {}        # pid -> {"orig": set, "set": set}
        self.last_poll = 0.0
        self.last_choose = 0.0
        self.last_tree = 0.0
        self.since = 0.0          # когда начали привязывать текущее окно (для уборки унаследованных привязок)
        self.cur_set = set()
        self.note = ""

    def pids(self):
        return set(self.pinned)

    def pin_entries(self):
        out = [{"tid": pid, "start": _start_of(pid), "set": sorted(st["set"]), "orig": sorted(st["orig"]),
                "whole_process": True, "since": None} for pid, st in self.pinned.items()]
        if self.cur_set:
            out.append({"tid": None, "set": sorted(self.cur_set), "orig": sorted(self.hw.cpus), "since": self.since})
        return out

    def tids(self):
        out = set()
        for pid in self.pinned:
            try:
                out.update(int(t) for t in os.listdir(f"/proc/{pid}/task"))
            except OSError:
                pass
        return out

    def status(self):
        return {"pid": self.pid, "name": self.name, "cores": self.cores, "procs": len(self.pinned), "note": self.note}

    # ---------- привязка ----------
    def _pin(self, pid, cpus):
        st = self.pinned.get(pid)
        try:
            if st is None:
                orig = os.sched_getaffinity(pid)
                if self.cur_set and orig == self.cur_set:
                    orig = set(self.hw.cpus)  # родился уже привязанным (унаследовал от нас) — исходная = все ядра
                new = orig & cpus
                if not new:
                    return  # пользователь сам запретил эти ядра
                st = self.pinned[pid] = {"orig": orig, "set": new}
            else:
                new = st["orig"] & cpus
                if not new:
                    return
                st["set"] = new
            for tid in _tids(pid):  # новые потоки появляются постоянно — проходим все
                try:
                    if os.sched_getaffinity(tid) != new:
                        os.sched_setaffinity(tid, new)
                except OSError:
                    pass
        except OSError:
            self.pinned.pop(pid, None)

    def _release(self, pid):
        st = self.pinned.pop(pid, None)
        if not st:
            return
        for tid in _tids(pid):
            try:
                if os.sched_getaffinity(tid) == st["set"]:
                    os.sched_setaffinity(tid, st["orig"])
            except OSError:
                pass

    def release_all(self, why=""):
        had = bool(self.pinned)
        for pid in list(self.pinned):
            self._release(pid)
        if had and self.cur_set:
            # процессы, запущенные из активного окна после последнего обхода дерева
            restore_inherited(self.since, self.cur_set, set(self.hw.cpus))
        self.cur_set = set()
        if had and why:
            self.event(f"активное окно: {self.name} отпущено ({why})")
        self.pid, self.cores = None, []

    # ---------- выбор ядер ----------
    def _choose(self, cfg, core_t, hot_cores, now, avoid=()):
        n = max(1, min(int(cfg["cores"]), len(core_t) - 1 if len(core_t) > 1 else 1))
        # сначала не под троттлингом, потом без припаркованных тяжёлых задач, потом самые холодные
        ranked = sorted(core_t, key=lambda c: (c in hot_cores, c in avoid, core_t[c]))
        want = sorted(ranked[:n])
        if not self.cores:
            return want
        if now - self.last_choose < cfg["recheck_s"]:
            return self.cores
        cur_max = max(core_t.get(c, 0) for c in self.cores)
        new_max = max(core_t[c] for c in want)
        if any(c in hot_cores for c in self.cores) or cur_max - new_max >= cfg["swap_margin_c"] \
                or (any(c in avoid for c in self.cores) and not any(c in avoid for c in want)):
            return want
        return self.cores

    def tick(self, cfg, core_t, hot_cores, avoid=()):
        now = time.monotonic()
        if not cfg or not core_t:
            if self.pinned:
                self.release_all("функция выключена")
            self.note = ""
            return
        if now - self.last_poll < 1.0:
            return
        self.last_poll = now
        pid, info = active_pid()
        if pid is None or pid == os.getpid():
            if self.pinned:
                self.release_all(info)
            self.note = info if pid is None else ""
            return
        self.note = ""
        if pid != self.pid:
            self.release_all("фокус на другом окне")
            self.pid, self.name = pid, f"{info} ({_comm(pid)})"
        cores = self._choose(cfg, core_t, hot_cores, now, avoid)
        changed = cores != self.cores or not self.pinned
        if cores != self.cores:
            self.cores = cores
            self.last_choose = now
            temps = "/".join(f"{core_t[c]:.0f}" for c in cores)
            self.event(f"активное окно: {self.name} → ядра {','.join(map(str, cores))} ({temps} °C)")
        # новые потоки и дочерние процессы наследуют привязку сами, поэтому дерево
        # обходим только при смене окна/ядер и раз в 5 с (вдруг кто-то сбросил привязку)
        if not changed and now - self.last_tree < 5:
            return
        self.last_tree = now
        cpus = {t for c in self.cores for t in self.hw.threads_of_core(c)}
        if cpus != self.cur_set:
            if self.cur_set:
                restore_inherited(self.since, self.cur_set, set(self.hw.cpus))
            self.cur_set, self.since = cpus, _uptime_ticks()
        # cur_set — это множество потоков ядер; у процессов привязка = orig & cur_set, для полной orig это cur_set
        tree = set(process_tree(pid))
        for p in tree:
            self._pin(p, cpus)
        for p in list(self.pinned):
            if p not in tree:
                self._release(p)
