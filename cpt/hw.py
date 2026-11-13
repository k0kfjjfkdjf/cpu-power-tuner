"""Доступ к железу: RAPL, TCC offset, intel_pstate, EPP и сбор статистики.

Все пути идут через P(), чтобы можно было тестировать на поддельном sysfs:
CPT_ROOT=/tmp/fake python3 -m cpt.daemon
"""
import glob
import os
import time

ROOT = os.environ.get("CPT_ROOT", "")

RAPL = "/sys/class/powercap/intel-rapl:0"
RAPL_MMIO = "/sys/class/powercap/intel-rapl-mmio:0"
PSTATE = "/sys/devices/system/cpu/intel_pstate"
CPU_DIR = "/sys/devices/system/cpu"
PPD_STATE = "/var/lib/power-profiles-daemon/state.ini"


def P(path):
    return ROOT + path


def rd(path, default=None):
    try:
        with open(P(path)) as f:
            return f.read().strip()
    except OSError:
        return default


def rd_int(path, default=None):
    v = rd(path)
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def wr(path, value):
    with open(P(path), "w") as f:
        f.write(str(value))


def exists(path):
    return os.path.exists(P(path))


def _strip(path):
    return path[len(ROOT):] if ROOT and path.startswith(ROOT) else path


def find_cooling(type_name):
    for d in sorted(glob.glob(P("/sys/class/thermal/cooling_device*"))):
        try:
            with open(d + "/type") as f:
                if f.read().strip() == type_name:
                    return _strip(d)
        except OSError:
            pass
    return None


def find_hwmon(name):
    for d in sorted(glob.glob(P("/sys/class/hwmon/hwmon*"))):
        try:
            with open(d + "/name") as f:
                if f.read().strip() == name:
                    return _strip(d)
        except OSError:
            pass
    return None


def find_gpu():
    for d in sorted(glob.glob(P("/sys/class/drm/card[0-9]"))):
        if os.path.exists(d + "/gt_act_freq_mhz"):
            return _strip(d)
    return None


def cpu_list():
    out = []
    for d in glob.glob(P(CPU_DIR + "/cpu[0-9]*")):
        n = os.path.basename(d)[3:]
        if n.isdigit() and os.path.isdir(d + "/cpufreq"):
            out.append(int(n))
    return sorted(out)


class Hardware:
    """Определяет, что есть на машине, и умеет читать/писать параметры."""

    def __init__(self):
        self.cpus = cpu_list()
        self.coretemp = find_hwmon("coretemp")
        self.tcc = find_cooling("TCC Offset")
        self.gpu = find_gpu()
        self.has_rapl = exists(RAPL + "/constraint_0_power_limit_uw")
        self.has_mmio = exists(RAPL_MMIO + "/constraint_0_power_limit_uw")
        self.has_no_turbo = exists(PSTATE + "/no_turbo")
        self.has_max_perf = exists(PSTATE + "/max_perf_pct")
        epp0 = f"{CPU_DIR}/cpu{self.cpus[0]}/cpufreq/energy_performance_preference" if self.cpus else ""
        self.has_epp = bool(epp0) and exists(epp0)
        self.epp_choices = (rd(f"{CPU_DIR}/cpu{self.cpus[0]}/cpufreq/energy_performance_available_preferences", "") or "").split() if self.has_epp else []
        self.tjmax = self._tjmax()
        self.tcc_max = rd_int(self.tcc + "/max_state", 0) if self.tcc else 0
        self.core_of = {c: rd_int(f"{CPU_DIR}/cpu{c}/topology/core_id", c) for c in self.cpus}
        self.max_freq_khz = rd_int(f"{CPU_DIR}/cpu{self.cpus[0]}/cpufreq/cpuinfo_max_freq", 0) if self.cpus else 0
        self.min_freq_khz = rd_int(f"{CPU_DIR}/cpu{self.cpus[0]}/cpufreq/cpuinfo_min_freq", 0) if self.cpus else 0
        self.base_freq_khz = rd_int(f"{CPU_DIR}/cpu{self.cpus[0]}/cpufreq/base_frequency", 0) if self.cpus else 0
        self.model = self._model()
        self.memo = {}

    # ---------- статичные сведения ----------
    def _tjmax(self):
        # coretemp отдаёт Tjmax как temp*_crit; MSR не нужен
        if self.coretemp:
            v = rd_int(self.coretemp + "/temp1_crit")
            if v:
                return v // 1000
        return 100

    def _model(self):
        try:
            with open(P("/proc/cpuinfo")) as f:
                for line in f:
                    if line.startswith("model name"):
                        return line.split(":", 1)[1].strip()
        except OSError:
            pass
        return "CPU"

    def capabilities(self):
        return {
            "model": self.model,
            "threads": len(self.cpus),
            "cores": len(set(self.core_of.values())),
            "rapl": self.has_rapl,
            "rapl_mmio": self.has_mmio,
            "tcc": bool(self.tcc),
            "tcc_max": self.tcc_max,
            "tjmax": self.tjmax,
            "no_turbo": self.has_no_turbo,
            "epp": self.has_epp,
            "epp_choices": self.epp_choices,
            "gpu": bool(self.gpu),
            "min_freq_mhz": self.min_freq_khz // 1000,
            "base_freq_mhz": self.base_freq_khz // 1000,
            "max_freq_mhz": self.max_freq_khz // 1000,
        }

    # ---------- текущие значения управляемых параметров ----------
    def read_controls(self):
        c = {}
        if self.has_rapl:
            c["pl1_uw"] = rd_int(RAPL + "/constraint_0_power_limit_uw")
            c["pl2_uw"] = rd_int(RAPL + "/constraint_1_power_limit_uw")
        if self.has_mmio:
            c["mmio_pl1_uw"] = rd_int(RAPL_MMIO + "/constraint_0_power_limit_uw")
            c["mmio_pl2_uw"] = rd_int(RAPL_MMIO + "/constraint_1_power_limit_uw")
        if self.tcc:
            c["tcc_offset"] = rd_int(self.tcc + "/cur_state")
        if self.has_no_turbo:
            c["no_turbo"] = rd_int(PSTATE + "/no_turbo")
        if self.has_epp:
            c["epp"] = self.read_epp()
        c["max_freq_khz"] = {cpu: rd_int(self._maxf(cpu)) for cpu in self.cpus}
        return c

    def read_epp(self):
        return rd(f"{CPU_DIR}/cpu{self.cpus[0]}/cpufreq/energy_performance_preference")

    def _maxf(self, cpu):
        return f"{CPU_DIR}/cpu{cpu}/cpufreq/scaling_max_freq"

    # ---------- запись ----------
    # Ядро может округлить или зажать значение (RAPL с шагом 0.125 Вт, scaling_max_freq при no_turbo).
    # Чтобы не переписывать одно и то же каждые 5 секунд, помним, что прочиталось после нашей записи.
    def ensure(self, path, value):
        value = str(value)
        cur = rd(path)
        m = self.memo.get(path)
        if cur == value or (m and m[0] == value and m[1] == cur):
            return False
        wr(path, value)
        self.memo[path] = (value, rd(path))
        return True

    def set_rapl(self, pl1_uw, pl2_uw):
        p1, p2 = RAPL + "/constraint_0_power_limit_uw", RAPL + "/constraint_1_power_limit_uw"
        cur2 = rd_int(p2) or 0
        order = [(p1, pl1_uw, "PL1"), (p2, pl2_uw, "PL2")]
        if pl1_uw > cur2:  # сначала поднять PL2, чтобы PL1 не оказался выше
            order.reverse()
        return [f"{n}={v / 1e6:g}W" for path, v, n in order if self.ensure(path, v)]

    def set_tcc_offset(self, offset):
        offset = max(0, min(int(offset), self.tcc_max))
        return [f"TCC offset={offset}"] if self.ensure(self.tcc + "/cur_state", offset) else []

    def set_no_turbo(self, value):
        value = 1 if value else 0
        return [f"no_turbo={value}"] if self.ensure(PSTATE + "/no_turbo", value) else []

    def set_max_freq(self, per_cpu_khz):
        changed = [c for c, khz in per_cpu_khz.items() if self.ensure(self._maxf(c), int(khz))]
        if not changed:
            return []
        vals = sorted({per_cpu_khz[c] // 1000 for c in changed})
        return [f"max_freq CPU{','.join(map(str, changed))}={'/'.join(map(str, vals))}МГц"]

    def set_epp(self, value):
        changed = False
        for c in self.cpus:
            if self.ensure(f"{CPU_DIR}/cpu{c}/cpufreq/energy_performance_preference", value):
                changed = True
        return [f"EPP={value}"] if changed else []

    def threads_of_core(self, core):
        return [c for c, k in self.core_of.items() if k == core]

    def temps(self):
        """(температуры ядер {core: °C}, температура пакета)."""
        cores, pkg = {}, None
        d = self.coretemp
        if not d:
            return cores, pkg
        for lab in glob.glob(P(d + "/temp*_label")):
            try:
                with open(lab) as f:
                    label = f.read().strip()
                with open(lab.replace("_label", "_input")) as f:
                    t = int(f.read().strip()) / 1000
            except (OSError, ValueError):
                continue
            if label.startswith("Package"):
                pkg = t
            elif label.startswith("Core "):
                try:
                    cores[int(label.split()[1])] = t
                except ValueError:
                    pass
        return cores, pkg


class Sampler:
    """Считает нагрузку и мощность как разницу между двумя вызовами sample()."""

    def __init__(self, hw: Hardware):
        self.hw = hw
        self.prev_stat = self._proc_stat()
        self.prev_energy = self._energy()
        self.prev_t = time.monotonic()
        self.energy_range = rd_int(RAPL + "/max_energy_range_uj", 0) or 0

    def _proc_stat(self):
        out = {}
        try:
            with open(P("/proc/stat")) as f:
                for line in f:
                    if line.startswith("cpu") and line[3].isdigit():
                        parts = line.split()
                        vals = list(map(int, parts[1:]))
                        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
                        out[int(parts[0][3:])] = (sum(vals[:8]), idle)
        except OSError:
            pass
        return out

    def _energy(self):
        return rd_int(RAPL + "/energy_uj")

    def sample(self):
        hw = self.hw
        now = time.monotonic()
        dt = max(now - self.prev_t, 1e-3)

        stat = self._proc_stat()
        energy = self._energy()
        core_t, pkg_t = hw.temps()

        threads = []
        for c in hw.cpus:
            load = None
            if c in stat and c in self.prev_stat:
                tot = stat[c][0] - self.prev_stat[c][0]
                idl = stat[c][1] - self.prev_stat[c][1]
                load = round(100 * (tot - idl) / tot, 1) if tot > 0 else 0.0
            khz = rd_int(f"{CPU_DIR}/cpu{c}/cpufreq/scaling_cur_freq", 0) or 0
            core = hw.core_of.get(c, c)
            threads.append({
                "cpu": c,
                "core": core,
                "load": load,
                "mhz": khz // 1000,
                "temp": core_t.get(core),
            })

        power = None
        if energy is not None and self.prev_energy is not None:
            de = energy - self.prev_energy
            if de < 0 and self.energy_range:
                de += self.energy_range
            if de >= 0:
                power = round(de / dt / 1e6, 2)

        self.prev_stat, self.prev_energy, self.prev_t = stat, energy, now

        gpu_mhz = rd_int(hw.gpu + "/gt_act_freq_mhz") if hw.gpu else None
        loads = [t["load"] for t in threads if t["load"] is not None]
        return {
            "threads": threads,
            "pkg_temp": pkg_t,
            "core_temps": {str(k): v for k, v in sorted(core_t.items())},
            "pkg_power": power,
            "avg_load": round(sum(loads) / len(loads), 1) if loads else None,
            "avg_mhz": int(sum(t["mhz"] for t in threads) / len(threads)) if threads else None,
            "max_mhz": max((t["mhz"] for t in threads), default=None),
            "gpu_mhz": gpu_mhz,
            "controls": hw.read_controls(),
            "power_supply": power_supply(),
        }


def power_supply():
    ac, bat = None, None
    for d in glob.glob(P("/sys/class/power_supply/*")):
        typ = rd(_strip(d) + "/type")
        if typ == "Mains":
            v = rd_int(_strip(d) + "/online")
            if v is not None:
                ac = bool(v) or bool(ac)
        elif typ == "Battery" and bat is None:
            bat = {"capacity": rd_int(_strip(d) + "/capacity"), "status": rd(_strip(d) + "/status")}
    return {"ac": ac, "battery": bat}


def ppd_profile():
    """Текущий профиль power-profiles-daemon из его state.ini."""
    txt = rd(PPD_STATE)
    if not txt:
        return None
    for line in txt.splitlines():
        if line.startswith("Profile="):
            return line.split("=", 1)[1].strip()
    return None


def ppd_mtime():
    try:
        return os.stat(P(PPD_STATE)).st_mtime_ns
    except OSError:
        return None
