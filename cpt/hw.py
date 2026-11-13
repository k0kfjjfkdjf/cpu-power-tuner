"""Доступ к железу: что есть на машине, чтение/запись параметров, статистика.

Поддерживается:
  * лимиты мощности — Intel RAPL (powercap intel-rapl:0, плюс прошивочная intel-rapl-mmio:0);
  * аппаратный порог — Intel TCC offset (драйвер intel_tcc_cooling);
  * турбо — intel_pstate/no_turbo или общий cpufreq/boost (acpi-cpufreq, amd-pstate);
  * потолок частоты — scaling_max_freq каждого потока (любой cpufreq-драйвер, в т.ч. ARM);
  * EPP — energy_performance_preference (intel_pstate, amd-pstate в режиме active);
  * температуры — coretemp (Intel, по ядрам), k10temp/zenpower (AMD, по CCD), иначе термозона пакета;
  * топология — package_id + core_id (многопроцессорные системы, гибридные P/E-ядра).
Чего нет на машине — просто не показывается и не применяется.

Все пути идут через P(), чтобы можно было тестировать на поддельном sysfs:
CPT_ROOT=/tmp/fake python3 -m cpt.daemon
"""
import glob
import os
import re
import time

ROOT = os.environ.get("CPT_ROOT", "")

RAPL = "/sys/class/powercap/intel-rapl:0"
RAPL_MMIO = "/sys/class/powercap/intel-rapl-mmio:0"
PSTATE = "/sys/devices/system/cpu/intel_pstate"
BOOST = "/sys/devices/system/cpu/cpufreq/boost"
CPU_DIR = "/sys/devices/system/cpu"
PPD_STATE = "/var/lib/power-profiles-daemon/state.ini"

CPU_ZONES = ("x86_pkg_temp", "cpu-thermal", "cpu_thermal", "soc_thermal", "cpu0-thermal", "acpitz")


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
        if rd(_strip(d) + "/type") == type_name:
            return _strip(d)
    return None


def find_hwmons(*names):
    return [_strip(d) for d in sorted(glob.glob(P("/sys/class/hwmon/hwmon*"))) if rd(_strip(d) + "/name") in names]


def _labels(hwmon):
    """{label: путь к *_input} для датчиков температуры hwmon."""
    out = {}
    for lab in glob.glob(P(hwmon + "/temp*_label")):
        label = rd(_strip(lab))
        if label:
            out[label] = _strip(lab).replace("_label", "_input")
    return out


def _temp(path):
    v = rd_int(path)
    return v / 1000 if v is not None else None


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
        self.memo = {}
        self.model = self._model()
        self._topology()
        self._detect_temps()
        self.tcc = find_cooling("TCC Offset")
        self.tcc_max = rd_int(self.tcc + "/max_state", 0) if self.tcc else 0
        self.gpu_path, self.gpu_kind = self._find_gpu()
        self.has_rapl = exists(RAPL + "/constraint_0_power_limit_uw")
        self.has_mmio = exists(RAPL_MMIO + "/constraint_0_power_limit_uw")
        self.has_energy = exists(RAPL + "/energy_uj")
        if exists(PSTATE + "/no_turbo"):
            self.turbo_kind = "intel_pstate"
        elif exists(BOOST):
            self.turbo_kind = "boost"
        else:
            self.turbo_kind = None
        c0 = self.cpus[0] if self.cpus else 0
        self.driver = rd(f"{CPU_DIR}/cpu{c0}/cpufreq/scaling_driver", "?")
        self.has_epp = exists(f"{CPU_DIR}/cpu{c0}/cpufreq/energy_performance_preference")
        self.epp_choices = (rd(f"{CPU_DIR}/cpu{c0}/cpufreq/energy_performance_available_preferences", "") or "").split() \
            if self.has_epp else []
        # у гибридных процессоров (P/E-ядра) максимумы разные — храним по каждому потоку
        self.max_khz = {c: rd_int(f"{CPU_DIR}/cpu{c}/cpufreq/cpuinfo_max_freq", 0) or 0 for c in self.cpus}
        self.min_khz = {c: rd_int(f"{CPU_DIR}/cpu{c}/cpufreq/cpuinfo_min_freq", 0) or 0 for c in self.cpus}
        self.max_freq_khz = max(self.max_khz.values(), default=0)
        self.min_freq_khz = min((v for v in self.min_khz.values() if v), default=0)
        self.base_freq_khz = rd_int(f"{CPU_DIR}/cpu{c0}/cpufreq/base_frequency", 0) or 0

    # ---------- статичные сведения ----------
    def _model(self):
        try:
            with open(P("/proc/cpuinfo")) as f:
                for line in f:
                    if line.startswith(("model name", "Model", "Hardware")):
                        return line.split(":", 1)[1].strip()
        except OSError:
            pass
        return "CPU"

    def _topology(self):
        """core_of: поток -> ключ ядра. Ключ = core_id, а на многопроцессорных = package*1000 + core_id."""
        pkg = {c: rd_int(f"{CPU_DIR}/cpu{c}/topology/physical_package_id", 0) or 0 for c in self.cpus}
        cid = {c: rd_int(f"{CPU_DIR}/cpu{c}/topology/core_id", c) for c in self.cpus}
        multi = len(set(pkg.values())) > 1
        self.pkg_of = pkg
        self.core_of = {c: (pkg[c] * 1000 + cid[c]) if multi else cid[c] for c in self.cpus}
        self.cores = sorted(set(self.core_of.values()))

    def _detect_temps(self):
        """Выбирает источник температур. temp_source: coretemp | k10temp-ccd | package."""
        self.temp_map = {}        # ключ ядра -> путь к *_input
        self.pkg_temp_paths = []  # пути к температурам пакетов (берём максимум)
        self.tjmax = 100
        self.temp_source = "нет"
        multi = len(set(self.pkg_of.values())) > 1

        # Intel: coretemp, по одному hwmon на процессор
        cts = find_hwmons("coretemp")
        for hm in cts:
            labels = _labels(hm)
            pkg_id = 0
            for label, path in labels.items():
                m = re.match(r"Package id (\d+)", label)
                if m:
                    pkg_id = int(m.group(1))
                    self.pkg_temp_paths.append(path)
                    crit = rd_int(path.replace("_input", "_crit"))
                    if crit:
                        self.tjmax = crit // 1000
            for label, path in labels.items():
                m = re.match(r"Core (\d+)", label)
                if m:
                    key = pkg_id * 1000 + int(m.group(1)) if multi else int(m.group(1))
                    self.temp_map[key] = path
        if self.temp_map:
            self.temp_source = "coretemp (по ядрам)"
            self.per_core_temps = True
            return

        # AMD: k10temp / zenpower — Tctl/Tdie на пакет, Tccd1..N на кристаллы (CCD)
        for hm in find_hwmons("k10temp", "zenpower"):
            labels = _labels(hm)
            pkg_path = labels.get("Tdie") or labels.get("Tctl")
            if pkg_path:
                self.pkg_temp_paths.append(pkg_path)
            ccds = [labels[k] for k in sorted((k for k in labels if re.match(r"Tccd\d+", k)),
                                              key=lambda k: int(k[4:]))]
            if ccds:
                # ядра делим между CCD по порядку: точного соответствия в sysfs нет, это приближение
                per = max(1, -(-len(self.cores) // len(ccds)))
                for i, core in enumerate(self.cores):
                    self.temp_map[core] = ccds[min(i // per, len(ccds) - 1)]
                self.temp_source = f"k10temp (по {len(ccds)} CCD, приближённо)"
                self.per_core_temps = True
            elif pkg_path:
                self.temp_source = "k10temp (только пакет)"
            self.tjmax = 95
            if self.pkg_temp_paths:
                break

        # запасной вариант: термозона процессора (ARM и прочие)
        if not self.pkg_temp_paths:
            for z in sorted(glob.glob(P("/sys/class/thermal/thermal_zone*"))):
                z = _strip(z)
                if rd(z + "/type") in CPU_ZONES:
                    self.pkg_temp_paths.append(z + "/temp")
                    for tp in glob.glob(P(z + "/trip_point_*_type")):
                        if rd(_strip(tp)) == "critical":
                            v = rd_int(_strip(tp).replace("_type", "_temp"))
                            if v and v > 0:
                                self.tjmax = v // 1000
                    self.temp_source = f"термозона {rd(z + '/type')} (только пакет)"
                    break
        self.per_core_temps = bool(self.temp_map)

    def _find_gpu(self):
        for d in sorted(glob.glob(P("/sys/class/drm/card[0-9]"))):
            d = _strip(d)
            if exists(d + "/gt_act_freq_mhz"):                       # Intel i915
                return d + "/gt_act_freq_mhz", "i915"
            xe = glob.glob(P(d + "/device/tile0/gt0/freq0/act_freq"))
            if xe:                                                   # Intel xe
                return _strip(xe[0]), "xe"
            if exists(d + "/device/pp_dpm_sclk"):                    # AMD amdgpu
                return d + "/device/pp_dpm_sclk", "amdgpu"
        return None, None

    def capabilities(self):
        return {
            "model": self.model,
            "driver": self.driver,
            "threads": len(self.cpus),
            "cores": len(self.cores),
            "packages": len(set(self.pkg_of.values())),
            "hybrid": len(set(self.max_khz.values())) > 1,
            "rapl": self.has_rapl,
            "rapl_mmio": self.has_mmio,
            "energy": self.has_energy,
            "tcc": bool(self.tcc),
            "tcc_max": self.tcc_max,
            "tjmax": self.tjmax,
            "turbo": bool(self.turbo_kind),
            "turbo_kind": self.turbo_kind,
            "epp": self.has_epp,
            "epp_choices": self.epp_choices,
            "gpu": bool(self.gpu_path),
            "gpu_kind": self.gpu_kind,
            "temp_source": self.temp_source,
            "per_core_temps": self.per_core_temps,
            "min_freq_mhz": self.min_freq_khz // 1000,
            "base_freq_mhz": self.base_freq_khz // 1000,
            "max_freq_mhz": self.max_freq_khz // 1000,
        }

    # ---------- текущие значения управляемых параметров ----------
    def read_turbo(self):
        if self.turbo_kind == "intel_pstate":
            v = rd_int(PSTATE + "/no_turbo")
            return None if v is None else not v
        if self.turbo_kind == "boost":
            v = rd_int(BOOST)
            return None if v is None else bool(v)
        return None

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
        if self.turbo_kind:
            c["turbo"] = self.read_turbo()
        if self.has_epp:
            c["epp"] = self.read_epp()
        c["max_freq_khz"] = {cpu: rd_int(self._maxf(cpu)) for cpu in self.cpus}
        return c

    def read_epp(self):
        return rd(f"{CPU_DIR}/cpu{self.cpus[0]}/cpufreq/energy_performance_preference")

    def _maxf(self, cpu):
        return f"{CPU_DIR}/cpu{cpu}/cpufreq/scaling_max_freq"

    # ---------- запись ----------
    # Ядро может округлить или зажать значение (RAPL с шагом 0.125 Вт, scaling_max_freq при выключенном турбо).
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

    def set_turbo(self, enabled):
        enabled = bool(enabled)
        if self.turbo_kind == "intel_pstate":
            ch = self.ensure(PSTATE + "/no_turbo", 0 if enabled else 1)
        elif self.turbo_kind == "boost":
            ch = self.ensure(BOOST, 1 if enabled else 0)
        else:
            return []
        return [f"турбо={'вкл' if enabled else 'выкл'}"] if ch else []

    def set_max_freq(self, per_cpu_khz):
        changed = [c for c, khz in per_cpu_khz.items() if self.ensure(self._maxf(c), int(khz))]
        if not changed:
            return []
        vals = sorted({per_cpu_khz[c] // 1000 for c in changed})
        return [f"max_freq CPU{','.join(map(str, changed))}={'/'.join(map(str, vals))}МГц"]

    def set_epp(self, value):
        """value — строка для всех потоков или {поток: строка} (режим фокуса)."""
        per = value if isinstance(value, dict) else {c: value for c in self.cpus}
        changed = [c for c, v in per.items()
                   if v and self.ensure(f"{CPU_DIR}/cpu{c}/cpufreq/energy_performance_preference", v)]
        if not changed:
            return []
        vals = sorted({per[c] for c in changed})
        return [f"EPP={'/'.join(vals)}" + ("" if len(changed) == len(self.cpus) else f" CPU{','.join(map(str, changed))}")]

    def threads_of_core(self, core):
        return [c for c, k in self.core_of.items() if k == core]

    # ---------- температуры ----------
    def pkg_temp(self):
        vals = [t for t in (_temp(p) for p in self.pkg_temp_paths) if t is not None]
        return max(vals) if vals else None

    def temps(self):
        """(температуры ядер {ключ ядра: °C}, температура пакета).
        Если датчиков по ядрам нет, у всех ядер температура пакета — функции «по ядрам» тогда
        просто не находят разницы и ничего не делают."""
        pkg = self.pkg_temp()
        cores = {}
        for core in self.cores:
            path = self.temp_map.get(core)
            t = _temp(path) if path else None
            cores[core] = t if t is not None else pkg
        cores = {k: v for k, v in cores.items() if v is not None}
        if pkg is None and cores:
            pkg = max(cores.values())
        return cores, pkg

    def gpu_mhz(self):
        if not self.gpu_path:
            return None
        if self.gpu_kind == "amdgpu":
            for line in (rd(self.gpu_path, "") or "").splitlines():
                if line.rstrip().endswith("*"):
                    m = re.search(r"(\d+)\s*Mhz", line, re.I)
                    return int(m.group(1)) if m else None
            return None
        return rd_int(self.gpu_path)


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
            threads.append({"cpu": c, "core": core, "load": load, "mhz": khz // 1000, "temp": core_t.get(core)})

        power = None
        if energy is not None and self.prev_energy is not None:
            de = energy - self.prev_energy
            if de < 0 and self.energy_range:
                de += self.energy_range
            if de >= 0:
                power = round(de / dt / 1e6, 2)

        self.prev_stat, self.prev_energy, self.prev_t = stat, energy, now

        loads = [t["load"] for t in threads if t["load"] is not None]
        return {
            "threads": threads,
            "pkg_temp": pkg_t,
            "core_temps": {str(k): v for k, v in sorted(core_t.items())} if hw.per_core_temps else {},
            "pkg_power": power,
            "avg_load": round(sum(loads) / len(loads), 1) if loads else None,
            "avg_mhz": int(sum(t["mhz"] for t in threads) / len(threads)) if threads else None,
            "max_mhz": max((t["mhz"] for t in threads), default=None),
            "gpu_mhz": hw.gpu_mhz(),
            "controls": hw.read_controls(),
            "power_supply": power_supply(),
        }


def power_supply():
    ac, bat = None, None
    for d in glob.glob(P("/sys/class/power_supply/*")):
        typ = rd(_strip(d) + "/type")
        if typ in ("Mains", "USB", "USB_C", "USB_PD"):
            v = rd_int(_strip(d) + "/online")
            if v is not None:
                ac = bool(v) or bool(ac)
        elif typ == "Battery" and bat is None and rd(_strip(d) + "/scope") != "Device":
            bat = {"capacity": rd_int(_strip(d) + "/capacity"), "status": rd(_strip(d) + "/status")}
    return {"ac": ac, "battery": bat}


def ppd_available():
    return exists(PPD_STATE)


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
