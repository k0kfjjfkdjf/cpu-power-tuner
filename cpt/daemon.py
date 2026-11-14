"""Служба (root).

Цепочка решений:
  профиль ppd -> привязанный пресет -> (если динамический) статический пресет по заряду
  -> целевые значения -> + оверлей умного троттлинга / буста по температуре -> запись в sysfs.

Цикл 0.5 с: смена профиля ppd, заряд батареи, температуры (для умного троттлинга).
Раз в verify_interval: перепроверка всех значений (их может откатить прошивка/ppd).
"""
import grp
import json
import logging
import os
import signal
import socketserver
import threading
import time

from . import config, focus, hw, park

SOCKET_PATH = os.environ.get("CPT_SOCKET", "/run/cpu-power-tuner/sock")
FACTORY_PATH = os.path.join(config.STATE_DIR, "factory.json")
PINS_PATH = os.path.join(config.STATE_DIR, "pins.json")   # что служба привязала к ядрам — для уборки после сбоя
TICK = 0.5
BOOST_HYST = 5  # °C
GRACE_HYST = 2  # °C: ядро в отсрочке считается остывшим только на 2 °C ниже порога (датчик шагает по 1 °C)

log = logging.getLogger("cpt")


class Tuner:
    def __init__(self):
        self.hw = hw.Hardware()
        self.lock = threading.RLock()
        self.cfg = config.load(self.hw.epp_choices)
        if not os.path.exists(config.CONFIG_PATH):
            config.save(self.cfg)
        self.factory = self._load_or_capture_factory()
        self.sampler = hw.Sampler(self.hw)
        self.profile = None              # профиль ppd (или выбранный вручную, если ppd нет)
        self.ppd_mtime = None
        self.manual = not hw.ppd_available()  # без power-profiles-daemon профиль выбирается в GUI/CLI
        self.epp_ppd = self.hw.read_epp() if self.hw.has_epp else None   # что ставит ppd
        self.epp_written = None          # что последним записали мы
        self.static_id = None            # какой статический пресет реально работает
        self.reason = ""
        self.battery = None
        self.on_ac = None
        # оверлей
        self.hot_until = {}              # core -> monotonic time
        self.grace_start = {}            # core -> когда началась отсрочка троттлинга (даём парковке остудить)
        self.boost_off = False
        self.last_changes, self.last_errors, self.last_apply = [], [], None
        self.events = []                 # последние события для GUI
        self.parker = park.Parker(self.hw, self.event)
        self.focus = focus.Focus(self.hw, self.event)

    def event(self, msg):
        log.info(msg)
        self.events.append(time.strftime("%H:%M:%S ") + msg)
        del self.events[:-30]

    # ---------- заводские значения ----------
    def _load_or_capture_factory(self):
        try:
            with open(FACTORY_PATH) as f:
                fac = json.load(f)
            if "no_turbo" in fac and "turbo" not in fac:  # формат старых версий
                fac["turbo"] = not fac.pop("no_turbo")
            return fac
        except (OSError, ValueError):
            pass
        c = self.hw.read_controls()
        fac = {k: c[k] for k in ("pl1_uw", "pl2_uw", "tcc_offset", "turbo") if c.get(k) is not None}
        fac["captured"] = time.strftime("%Y-%m-%d %H:%M:%S")
        os.makedirs(config.STATE_DIR, exist_ok=True)
        with open(FACTORY_PATH, "w") as f:
            json.dump(fac, f, indent=1)
        log.info("сохранены заводские значения: %s", fac)
        return fac

    # ---------- выбор пресета ----------
    def bound_id(self):
        return self.cfg["bindings"].get(self.profile or "balanced", "balanced")

    def resolve(self):
        """Обновляет self.static_id. Возвращает True, если сменился."""
        pid = self.bound_id()
        p = self.cfg["presets"].get(pid)
        if p is None:
            new, why = None, "пресет не найден"
        elif p["type"] == "dynamic":
            new, why = config.resolve_dynamic(p, self.battery, self.on_ac, self.static_id,
                                              self.cfg["general"]["hysteresis_pct"])
            why = f"«{p['name']}»: {why}"
        else:
            new, why = pid, "статический"
        changed = new != self.static_id
        if changed:
            name = self.cfg["presets"].get(new, {}).get("name", new)
            self.event(f"работает пресет «{name}» ({why})")
        self.static_id, self.reason = new, why
        return changed

    def preset(self):
        return self.cfg["presets"].get(self.static_id) or {}

    # ---------- цели ----------
    def targets(self):
        p, h, fac = self.preset(), self.hw, self.factory
        on = lambda k: bool(p.get(k, {}).get("enabled"))  # noqa: E731
        val = lambda k: p[k]["value"]  # noqa: E731
        t = {}
        if h.has_rapl and fac.get("pl1_uw") and fac.get("pl2_uw"):
            pl1 = int(val("pl1_w") * 1e6) if on("pl1_w") else fac["pl1_uw"]
            pl2 = int(val("pl2_w") * 1e6) if on("pl2_w") else fac["pl2_uw"]
            pl2 = max(pl1, pl2)
            if on("smart") and val("smart")["pkg_w"] > 0 and self.hot_cores():
                cap = int(val("smart")["pkg_w"] * 1e6)
                pl1, pl2 = min(pl1, cap), min(pl2, cap)
            t["rapl"] = (pl1, pl2)
        if h.tcc:
            if on("temp_limit_c"):
                t["tcc_offset"] = h.tjmax - int(val("temp_limit_c"))
            elif "tcc_offset" in fac:
                t["tcc_offset"] = fac["tcc_offset"]
        if h.turbo_kind:
            turbo = fac.get("turbo", True)
            if on("turbo"):
                turbo = bool(val("turbo"))
            if on("boost_temp_c") and self.boost_off:
                turbo = False
            t["turbo"] = turbo
        if h.cpus and h.max_freq_khz:
            # у гибридных процессоров у P- и E-ядер свой максимум: потолок не выше собственного максимума потока
            per = {c: (min(int(val("max_freq_mhz")) * 1000, h.max_khz[c]) if on("max_freq_mhz") else h.max_khz[c])
                   for c in h.cpus if h.max_khz.get(c)}
            fo = p.get("focus", {})
            fg = self.focus.fg_cpus() if fo.get("enabled") else set()
            drop = int(fo.get("value", {}).get("bg_drop_mhz", 0) or 0) * 1000 if fg else 0
            if drop:  # режим фокуса: отдыхающим ядрам потолок ниже
                for c in per:
                    if c not in fg:
                        per[c] = max(per[c] - drop, h.min_khz.get(c, 0))
            if on("smart"):
                for core in self.hot_cores():
                    for c in h.threads_of_core(core):
                        if c in per:
                            per[c] = min(per[c], max(int(val("smart")["core_mhz"]) * 1000, h.min_khz.get(c, 0)))
            t["max_freq"] = per
        if h.has_epp:
            base = val("epp") if on("epp") else self.epp_ppd
            fo = p.get("focus", {})
            fg = self.focus.fg_cpus() if fo.get("enabled") else set()
            if fg and fo.get("value", {}).get("bg_epp_power") and "power" in h.epp_choices:
                t["epp"] = {c: (base if c in fg else "power") for c in h.cpus}  # отдыхающие ядра ленивее
            else:
                t["epp"] = base
        return t

    def hot_cores(self):
        now = time.monotonic()
        return sorted(c for c, until in self.hot_until.items() if until > now)

    def apply(self, targets, why=""):
        changes, errors = [], []
        h = self.hw
        for key, fn in (("rapl", lambda v: h.set_rapl(*v)), ("tcc_offset", h.set_tcc_offset),
                        ("turbo", h.set_turbo), ("max_freq", h.set_max_freq), ("epp", h.set_epp)):
            v = targets.get(key)
            if v is None:
                continue
            try:
                changes += fn(v)
                if key == "epp":
                    self.epp_written = v.get(h.cpus[0]) if isinstance(v, dict) else v
            except OSError as e:
                errors.append(f"{key}: {e.strerror or e}")
        if changes:
            self.last_changes = changes
            self.last_apply = time.strftime("%H:%M:%S") + (f" ({why})" if why else "")
            log.info("%s: %s", why or "применено", ", ".join(changes))
        if errors and errors != self.last_errors:
            for e in errors:
                log.warning("ошибка записи %s", e)
        self.last_errors = errors
        return changes, errors

    def unpin(self, why, deep=False):
        n = park.restore_saved_pins(PINS_PATH)
        if deep:
            n += park.unpin_orphans(self.hw, keep=set(self.parker.parked) | self.focus.tids())
        if n:
            self.event(f"сброшено привязок к ядрам, оставшихся от прошлого запуска: {n} ({why})")
        return n

    def save_pins(self):
        entries = self.parker.pin_entries() + self.focus.pin_entries()
        if entries != getattr(self, "_last_pins", None):
            self._last_pins = entries
            try:
                if entries:
                    park.save_pins(PINS_PATH, entries)
                elif os.path.exists(PINS_PATH):
                    os.remove(PINS_PATH)
            except OSError as e:
                log.warning("не удалось записать %s: %s", PINS_PATH, e)

    def restore_factory(self):
        with self.lock:
            self.parker.release_all("служба остановлена")
            self.focus.release_all("служба остановлена")
            self.save_pins()
            f, h, t = self.factory, self.hw, {}
            if h.has_rapl and "pl1_uw" in f and "pl2_uw" in f:
                t["rapl"] = (f["pl1_uw"], f["pl2_uw"])
            if h.tcc and "tcc_offset" in f:
                t["tcc_offset"] = f["tcc_offset"]
            if h.turbo_kind and "turbo" in f:
                t["turbo"] = f["turbo"]
            if h.cpus and h.max_freq_khz:
                t["max_freq"] = {c: h.max_khz[c] for c in h.cpus if h.max_khz.get(c)}
            if h.has_epp and self.epp_ppd:
                t["epp"] = self.epp_ppd
            ch, _ = self.apply(t, "возврат заводских")
            return ch

    # ---------- датчики ----------
    def update_power(self):
        ps = hw.power_supply()
        self.battery = (ps.get("battery") or {}).get("capacity")
        self.on_ac = ps.get("ac")

    def update_thermal(self):
        """Обновляет оверлей. Возвращает True, если он изменился."""
        p = self.preset()
        # без датчиков по ядрам «умные» функции по ядрам не работают (у всех ядер одна температура)
        smart = p.get("smart", {}) if self.hw.per_core_temps else {}
        boost = p.get("boost_temp_c", {})
        before = (tuple(self.hot_cores()), self.boost_off)
        if not smart.get("enabled") and not boost.get("enabled"):
            self.hot_until.clear()
            self.grace_start.clear()
            self.boost_off = False
            return before != ((), False)
        cores, pkg = self.hw.temps()
        now = time.monotonic()
        if smart.get("enabled"):
            sv = smart["value"]
            grace = sv.get("grace_s", 0) if p.get("park", {}).get("enabled") else 0
            for core, t in cores.items():
                if t >= sv["trigger_c"]:
                    if grace > 0 and self.hot_until.get(core, 0) <= now:
                        start = self.grace_start.setdefault(core, now)
                        if start == now:
                            self.event(f"ядро {core}: {t:.0f} °C ≥ {sv['trigger_c']} — {grace:g} с парковка пытается остудить")
                        if now - start < grace:
                            continue
                        self.grace_start.pop(core, None)
                    if self.hot_until.get(core, 0) <= now:
                        self.event(f"ядро {core}: {t:.0f} °C ≥ {sv['trigger_c']} — режу до {sv['core_mhz']} МГц на {sv['hold_s']:g} с")
                    self.hot_until[core] = now + sv["hold_s"]
                elif core in self.grace_start and t <= sv["trigger_c"] - GRACE_HYST:
                    self.grace_start.pop(core)
                    self.event(f"ядро {core}: остыло до {t:.0f} °C без троттлинга")
        else:
            self.hot_until.clear()
            self.grace_start.clear()
        if boost.get("enabled") and pkg is not None:
            lim = boost["value"]
            if not self.boost_off and pkg >= lim:
                self.boost_off = True
                self.event(f"пакет {pkg:.0f} °C ≥ {lim} — буст выключен")
            elif self.boost_off and pkg <= lim - BOOST_HYST:
                self.boost_off = False
                self.event(f"пакет {pkg:.0f} °C — буст снова включён")
        else:
            self.boost_off = False
        return (tuple(self.hot_cores()), self.boost_off) != before

    # ---------- основной цикл ----------
    PROFILE_FILE = os.path.join(config.STATE_DIR, "profile")

    def set_manual_profile(self, prof):
        if prof not in config.PPD_PROFILES:
            raise ValueError(f"неизвестный профиль {prof!r}")
        os.makedirs(config.STATE_DIR, exist_ok=True)
        with open(self.PROFILE_FILE, "w") as f:
            f.write(prof)
        self.event(f"профиль (вручную): {self.profile} → {prof}")
        self.profile = prof

    def poll_ppd(self):
        if self.manual:
            if self.profile is None:
                try:
                    with open(self.PROFILE_FILE) as f:
                        prof = f.read().strip()
                except OSError:
                    prof = "balanced"
                self.profile = prof if prof in config.PPD_PROFILES else "balanced"
                self.event(f"power-profiles-daemon не найден — профиль выбирается вручную: {self.profile}")
                return True
            return False
        mt = hw.ppd_mtime()
        if mt == self.ppd_mtime:
            return False
        self.ppd_mtime = mt
        prof = hw.ppd_profile()
        if prof == self.profile:
            return False
        time.sleep(0.4)  # даём ppd дописать EPP
        if self.hw.has_epp:
            self.epp_ppd = self.hw.read_epp()
            self.epp_written = None
        self.event(f"профиль ppd: {self.profile} → {prof}")
        self.profile = prof
        return True

    def verify(self):
        if self.hw.has_epp:
            cur = self.hw.read_epp()
            if cur != self.epp_written:
                self.epp_ppd = cur  # это поставил не я, а ppd (например, при смене батарея/сеть)
        self.apply(self.targets(), "возврат откаченных значений")

    def run(self, stop):
        try:
            self.unpin("старт службы")  # следы прошлых запусков (сбой, старая версия)
        except Exception:  # уборка не должна мешать запуску
            log.exception("уборка привязок при старте")
        self.update_power()
        if not self.poll_ppd() and self.profile is None:  # ppd есть, но профиль не прочитался
            log.warning("не найден профиль ppd (%s), беру balanced", hw.PPD_STATE)
            self.profile = "balanced"
        with self.lock:
            self.resolve()
            self.update_thermal()
            self.apply(self.targets(), "старт")
        last_verify = last_power = time.monotonic()
        last_hot = ()
        while not stop.wait(TICK):
            try:
                with self.lock:
                    now = time.monotonic()
                    why = None
                    if self.poll_ppd():
                        why = "смена профиля ppd"
                    if now - last_power >= 2:
                        last_power = now
                        self.update_power()
                    if self.resolve():
                        why = why or "смена ступени"
                    if self.update_thermal():
                        why = why or "умный троттлинг"
                    pk = self.preset().get("park", {}) if self.hw.per_core_temps else {}
                    fc = self.preset().get("focus", {})
                    core_t = self.hw.temps()[0] if (pk.get("enabled") or fc.get("enabled")) else {}
                    if self.focus.tick(fc["value"] if fc.get("enabled") else None, core_t, self.hot_cores(),
                                       avoid={p["to"] for p in self.parker.parked.values()}):
                        self.apply(self.targets(), "режим фокуса")
                    self.parker.tick(pk["value"] if pk.get("enabled") else None, core_t, self.hot_cores(),
                                     skip_pids=self.focus.pids(), skip_cores=self.focus.cores,
                                     evac=list(self.grace_start))
                    self.save_pins()
                    hot = tuple(self.hot_cores())
                    if hot != last_hot:  # ядро остыло (время вышло) — снять ограничение
                        last_hot = hot
                        why = why or "умный троттлинг"
                    if why:
                        self.apply(self.targets(), why)
                    elif now - last_verify >= self.cfg["general"]["verify_interval"]:
                        last_verify = now
                        self.verify()
            except Exception:
                log.exception("ошибка в цикле")

    # ---------- API ----------
    def handle(self, req):
        cmd = req.get("cmd")
        if cmd == "status":
            with self.lock:
                s = self.sampler.sample()
                s.update({
                    "profile": self.profile, "manual_profile": self.manual, "bound_preset": self.bound_id(), "static_preset": self.static_id,
                    "reason": self.reason, "last_apply": self.last_apply, "last_changes": self.last_changes,
                    "errors": self.last_errors, "epp_ppd": self.epp_ppd,
                    "hot_cores": self.hot_cores(), "boost_off": self.boost_off,
                    "parked": self.parker.status(),
                    "grace": {str(c): round(time.monotonic() - t, 1) for c, t in self.grace_start.items()},
                    "focus": self.focus.status(),
                    "events": self.events[-8:],
                })
            return {"ok": True, "status": s}
        if cmd == "caps":
            return {"ok": True, "caps": self.hw.capabilities(), "factory": self.factory,
                    "params": {k: list(v[1:]) for k, v in config.PARAMS.items()},
                    "dict_fields": {d: {k: list(v[1:]) for k, v in f.items()} for d, f in config.DICT_FIELDS.items()},
                    "templates": config.TEMPLATES, "ppd_profiles": config.PPD_PROFILES,
                    "ppd_titles": config.PPD_TITLES}
        if cmd == "get_config":
            return {"ok": True, "config": self.cfg}
        if cmd in ("set_config", "reset_all"):
            new = config.defaults() if cmd == "reset_all" else config.validate(req.get("config", {}), self.hw.epp_choices)
            with self.lock:
                self.cfg = new
                config.save(new)
                self.event("все пресеты сброшены к стандартным" if cmd == "reset_all" else "конфиг обновлён")
                self.resolve()
                self.update_thermal()
                ch, err = self.apply(self.targets(), "новый конфиг")
            return {"ok": True, "config": self.cfg, "changes": ch, "errors": err}
        if cmd == "set_profile":
            if not self.manual:
                return {"ok": False, "error": "профиль переключается через power-profiles-daemon (powerprofilesctl)"}
            with self.lock:
                self.set_manual_profile(req.get("profile"))
                self.resolve()
                self.update_thermal()
                ch, err = self.apply(self.targets(), "смена профиля")
            return {"ok": True, "changes": ch, "errors": err}
        if cmd == "unpin":
            with self.lock:
                return {"ok": True, "count": self.unpin("вручную", deep=True)}
        if cmd == "apply":
            with self.lock:
                self.cfg = config.load(self.hw.epp_choices)  # подхватить ручные правки config.toml
                self.resolve()
                self.update_thermal()
                self.hw.memo.clear()
                ch, err = self.apply(self.targets(), "вручную")
            return {"ok": True, "changes": ch, "errors": err}
        return {"ok": False, "error": f"неизвестная команда {cmd!r}"}


def serve(tuner):
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            for line in self.rfile:
                try:
                    resp = tuner.handle(json.loads(line))
                except Exception as e:  # noqa: BLE001
                    log.exception("запрос")
                    resp = {"ok": False, "error": str(e)}
                self.wfile.write((json.dumps(resp, ensure_ascii=False) + "\n").encode())
                self.wfile.flush()

    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

    os.makedirs(os.path.dirname(SOCKET_PATH), exist_ok=True)
    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass
    srv = Server(SOCKET_PATH, Handler)
    group = tuner.cfg["general"]["socket_group"]
    for g in (group, "wheel", "sudo", "admin"):  # wheel — Arch/Fedora, sudo — Debian/Ubuntu
        try:
            grp.getgrnam(g)
            group = g
            break
        except KeyError:
            continue
    try:
        gid = grp.getgrnam(group).gr_gid
        os.chown(SOCKET_PATH, 0 if os.geteuid() == 0 else -1, gid)
        os.chmod(SOCKET_PATH, 0o660)
    except (KeyError, PermissionError) as e:
        log.warning("не удалось выдать сокет группе %s: %s", group, e)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("сокет %s (группа %s)", SOCKET_PATH, group)
    return srv


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if not hw.ROOT and os.geteuid() != 0:
        raise SystemExit("служба должна работать от root")
    if not hw.ROOT:
        os.system("modprobe intel_tcc_cooling 2>/dev/null")
    tuner = Tuner()
    h = tuner.hw
    log.info("%s | потоков %d | RAPL %s | TCC %s (Tjmax %d) | EPP %s", h.model, len(h.cpus), h.has_rapl,
             bool(h.tcc), h.tjmax, h.epp_choices)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    srv = serve(tuner)
    try:
        tuner.run(stop)
    finally:
        srv.shutdown()
        if tuner.cfg["general"]["restore_on_stop"]:
            tuner.restore_factory()
        try:
            os.unlink(SOCKET_PATH)
        except OSError:
            pass


if __name__ == "__main__":
    main()
