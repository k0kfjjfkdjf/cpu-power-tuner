"""Конфиг v2: список пресетов (статических и динамических) + привязки к профилям ppd.

Статический пресет: набор параметров, у каждого {enabled, value}. enabled=false = «не трогать»
(заводское значение, для EPP — то, что ставит ppd).

Динамический пресет: выбирает статический пресет по заряду батареи:
  steps = [{below = 20, preset = "balanced"}, {below = 15, preset = "eco"}]
  при заряде ≤ below % действует ступень с наименьшим подходящим порогом;
  выше всех порогов — default; от сети — on_ac (пусто = как default).
"""
import copy
import os
import re
import tomllib

CONFIG_PATH = os.environ.get("CPT_CONFIG", "/etc/cpu-power-tuner/config.toml")
STATE_DIR = os.environ.get("CPT_STATE", "/var/lib/cpu-power-tuner")

PPD_PROFILES = ["power-saver", "balanced", "performance"]
PPD_TITLES = {"power-saver": "Экономия", "balanced": "Баланс", "performance": "Производительность"}

SMART_FIELDS = {  # поле: (тип, мин, макс, шаг, подпись)
    "trigger_c": (int, 50, 105, 1, "срабатывает при t ядра ≥, °C"),
    "hold_s": (float, 0.5, 30, 0.5, "держать ограничение, с"),
    "core_mhz": (int, 400, 6000, 100, "частота горячего ядра, МГц"),
    "pkg_w": (float, 0, 64, 0.5, "заодно лимит пакета, Вт (0 = нет)"),
    "grace_s": (float, 0, 30, 0.5, "сначала дать парковке остудить, с (0 = сразу)"),
}

PARK_FIELDS = {
    "trigger_c": (int, 50, 105, 1, "парковать, когда t ядра ≥, °C"),
    "hold_s": (float, 0, 600, 1, "держать на холодном ядре, с (0 = пока оно не нагреется)"),
    "min_load_pct": (int, 20, 100, 5, "только задачи, грузящие ядро ≥, %"),
}
FOCUS_FIELDS = {
    "cores": (int, 1, 16, 1, "сколько самых холодных ядер отдать"),
    "recheck_s": (float, 1, 60, 1, "пересматривать выбор ядер раз в, с"),
    "swap_margin_c": (int, 1, 20, 1, "переезжать, если другие холоднее на, °C"),
    "rotate_s": (float, 0, 600, 5, "ротация групп по таймеру, с (0 = только по нагреву)"),
    "nice": (int, -20, 0, 1, "приоритет окна, nice (0 = не трогать)"),
    "comp_nice": (int, -20, 0, 1, "приоритет композитора, nice (0 = не трогать)"),
    "bg_drop_mhz": (int, 0, 3000, 100, "отдыхающим ядрам минус к потолку, МГц (0 = нет)"),
    "bg_epp_power": (int, 0, 1, 1, "EPP power отдыхающим ядрам (1 = да)"),
}
DICT_FIELDS = {"smart": SMART_FIELDS, "park": PARK_FIELDS, "focus": FOCUS_FIELDS}

# key: (тип, мин, макс, шаг, подпись, пояснение)
PARAMS = {
    "pl1_w": (float, 3.0, 64.0, 0.5, "Длительный лимит мощности PL1, Вт",
              "Средняя мощность пакета за ~28 с. Главный рычаг тишины и автономности."),
    "pl2_w": (float, 3.0, 64.0, 0.5, "Пиковый лимит мощности PL2, Вт",
              "Короткие всплески. Равный PL1 — никаких скачков нагрева."),
    "temp_limit_c": (int, 40, 100, 1, "Аппаратный порог троттлинга, °C",
                     "TCC offset: процессор сам режет частоту выше порога. Грубо, но надёжно — как страховка."),
    "turbo": (bool, None, None, None, "Турбо-буст",
              "Выключить — частота не выше базовой."),
    "max_freq_mhz": (int, 400, 6000, 100, "Потолок буста, МГц",
                     "Максимальная частота всех ядер (scaling_max_freq)."),
    "boost_temp_c": (int, 50, 100, 1, "Буст только пока пакет холоднее, °C",
                     "Выше порога турбо выключается, включается обратно на 5 °C ниже."),
    "epp": (str, None, None, None, "Энергопрофиль ядра (EPP)",
            "Насколько охотно ядро поднимает частоту. power — максимум экономии."),
    "smart": (dict, None, None, None, "Умный троттлинг горячих ядер",
              "Если ядро нагрелось до порога, его потокам на несколько секунд режется частота, "
              "чтобы оно остыло. Остальные ядра работают как обычно."),
    "park": (dict, None, None, None, "Умная парковка тяжёлых задач",
             "Пока ядро только подбирается к перегреву, самая тяжёлая задача на нём переезжает на самое "
             "холодное свободное ядро (через привязку к ядрам), а потом отпускается обратно. "
             "Ставьте порог на несколько градусов ниже умного троттлинга."),
    "focus": (dict, None, None, None, "Активное окно и режим фокуса",
              "Ядра делятся на рабочую группу и отдыхающую. Программа в фокусе (с дочерними процессами) "
              "привязывается к самым холодным ядрам и получает повышенный приоритет; отдыхающим ядрам — "
              "потолок ниже и EPP power, фон там работает медленнее и холоднее. Группы меняются ролями "
              "по нагреву или по таймеру. Композитор к ядрам не привязывается (это унаследуют все программы), "
              "только приоритет. Hyprland и Sway/i3."),
}


def _p(enabled, value):
    return {"enabled": enabled, "value": value}


def _static(name, builtin, **over):
    base = {
        "pl1_w": _p(False, 15.0), "pl2_w": _p(False, 40.0), "temp_limit_c": _p(False, 95),
        "turbo": _p(False, True), "max_freq_mhz": _p(False, 3600), "boost_temp_c": _p(False, 85),
        "epp": _p(False, "balance_performance"),
        "smart": _p(False, {"trigger_c": 90, "hold_s": 2.0, "core_mhz": 1500, "pkg_w": 0.0, "grace_s": 0.0}),
        "park": _p(False, {"trigger_c": 85, "hold_s": 10.0, "min_load_pct": 60}),
        "focus": _p(False, {"cores": 2, "recheck_s": 5.0, "swap_margin_c": 4, "rotate_s": 0.0, "nice": -5,
                            "comp_nice": 0, "bg_drop_mhz": 200, "bg_epp_power": 1}),
    }
    base.update(over)
    return {"name": name, "type": "static", "builtin": builtin, **base}


TEMPLATES = {
    "eco": _static("Экономия", "eco",
                   pl1_w=_p(True, 7.0), pl2_w=_p(True, 8.0), temp_limit_c=_p(True, 70),
                   epp=_p(True, "power"),
                   smart=_p(True, {"trigger_c": 75, "hold_s": 3.0, "core_mhz": 800, "pkg_w": 0.0, "grace_s": 2.0}),
                   park=_p(True, {"trigger_c": 70, "hold_s": 15.0, "min_load_pct": 50})),
    "quiet": _static("Тихий", "quiet",
                     pl1_w=_p(True, 8.0), pl2_w=_p(True, 10.0), boost_temp_c=_p(True, 70),
                     max_freq_mhz=_p(True, 2500), epp=_p(True, "balance_power"),
                     smart=_p(True, {"trigger_c": 78, "hold_s": 2.0, "core_mhz": 1200, "pkg_w": 7.0, "grace_s": 3.0}),
                     park=_p(True, {"trigger_c": 73, "hold_s": 10.0, "min_load_pct": 50})),
    "balanced": _static("Баланс", "balanced",
                        pl1_w=_p(True, 12.0), pl2_w=_p(True, 20.0), temp_limit_c=_p(True, 85),
                        smart=_p(True, {"trigger_c": 90, "hold_s": 2.0, "core_mhz": 1500, "pkg_w": 0.0, "grace_s": 3.0}),
                        park=_p(True, {"trigger_c": 85, "hold_s": 10.0, "min_load_pct": 60})),
    "performance": _static("Производительность", "performance"),
    "auto_battery": {
        "name": "Авто по батарее", "type": "dynamic", "builtin": "auto_battery",
        "on_ac": "", "default": "balanced",
        "steps": [{"below": 50, "preset": "quiet"}, {"below": 20, "preset": "eco"}],
    },
}

DEFAULT_GENERAL = {
    "socket_group": "wheel",
    "verify_interval": 5,
    "restore_on_stop": True,
    "hysteresis_pct": 2,      # динамические пресеты: на сколько % зарядиться, чтобы вернуться на ступень выше
}
DEFAULT_BINDINGS = {"power-saver": "eco", "balanced": "balanced", "performance": "performance"}


def defaults():
    return {"general": dict(DEFAULT_GENERAL), "bindings": dict(DEFAULT_BINDINGS),
            "presets": copy.deepcopy(TEMPLATES)}


def template(builtin):
    return copy.deepcopy(TEMPLATES.get(builtin)) if builtin in TEMPLATES else None


def _num(typ, v, lo, hi):
    v = typ(v)
    return max(lo, min(hi, v))


def _validate_static(src, epp_choices):
    b = src.get("builtin") if src.get("builtin") in TEMPLATES and TEMPLATES[src.get("builtin")]["type"] == "static" else None
    out = template(b) if b else _static("Пресет", None)
    out["name"] = str(src.get("name") or out["name"])[:60]
    for key, (typ, lo, hi, _s, _l, _h) in PARAMS.items():
        item = src.get(key)
        if not isinstance(item, dict):
            continue
        dst = out[key]
        if isinstance(item.get("enabled"), bool):
            dst["enabled"] = item["enabled"]
        v = item.get("value")
        try:
            if typ is bool:
                if isinstance(v, bool):
                    dst["value"] = v
            elif typ is str:
                if isinstance(v, str) and (not epp_choices or v in epp_choices):
                    dst["value"] = v
            elif typ is dict:
                if isinstance(v, dict):
                    for f, (ft, flo, fhi, _st, _lb) in DICT_FIELDS[key].items():
                        if f in v:
                            try:
                                dst["value"][f] = _num(ft, v[f], flo, fhi)
                            except (TypeError, ValueError):
                                pass
            else:
                dst["value"] = _num(typ, v, lo, hi)
        except (TypeError, ValueError):
            pass
    if out["pl2_w"]["value"] < out["pl1_w"]["value"]:
        out["pl2_w"]["value"] = out["pl1_w"]["value"]
    return out


def _validate_dynamic(src, static_ids):
    first = static_ids[0] if static_ids else ""
    out = {"name": str(src.get("name") or "Динамический")[:60], "type": "dynamic",
           "builtin": src.get("builtin") if src.get("builtin") in TEMPLATES else None,
           "on_ac": src.get("on_ac") if src.get("on_ac") in static_ids else "",
           "default": src.get("default") if src.get("default") in static_ids else first,
           "steps": []}
    seen = set()
    for st in src.get("steps", []) if isinstance(src.get("steps"), list) else []:
        try:
            below = int(st.get("below"))
        except (TypeError, ValueError, AttributeError):
            continue
        below = max(1, min(100, below))
        if st.get("preset") in static_ids and below not in seen:
            seen.add(below)
            out["steps"].append({"below": below, "preset": st["preset"]})
    out["steps"].sort(key=lambda s: -s["below"])
    return out


ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")


def validate(cfg, epp_choices=None):
    out = defaults()
    if not isinstance(cfg, dict):
        return out
    g = cfg.get("general", {})
    if isinstance(g, dict):
        for k, dv in DEFAULT_GENERAL.items():
            v = g.get(k)
            if isinstance(dv, bool):
                if isinstance(v, bool):
                    out["general"][k] = v
            elif isinstance(dv, int):
                if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
                    out["general"][k] = int(v)
            elif isinstance(v, str):
                out["general"][k] = v

    presets = cfg.get("presets")
    if isinstance(presets, dict) and presets:
        src = {k: v for k, v in presets.items() if isinstance(k, str) and ID_RE.match(k) and isinstance(v, dict)}
        # встроенные шаблоны нельзя удалить: если пропали — возвращаем
        for b, t in TEMPLATES.items():
            if b not in src:
                src[b] = copy.deepcopy(t)
        static_ids = [k for k, v in src.items() if v.get("type", "static") != "dynamic"]
        res = {}
        for k, v in src.items():
            if v.get("type") == "dynamic":
                res[k] = _validate_dynamic(v, static_ids)
            else:
                res[k] = _validate_static(v, epp_choices)
        out["presets"] = res

    b = cfg.get("bindings", {})
    if isinstance(b, dict):
        for prof in PPD_PROFILES:
            if b.get(prof) in out["presets"]:
                out["bindings"][prof] = b[prof]
    return out


def load(epp_choices=None):
    try:
        with open(CONFIG_PATH, "rb") as f:
            raw = tomllib.load(f)
    except FileNotFoundError:
        return defaults()
    except tomllib.TOMLDecodeError as e:
        import logging
        logging.getLogger("cpt").error("конфиг повреждён (%s), беру стандартный", e)
        return defaults()
    return validate(raw, epp_choices)


def _tv(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k} = {_tv(x)}" for k, x in v.items()) + " }"
    if isinstance(v, list):
        return "[" + ", ".join(_tv(x) for x in v) + "]"
    if v is None:
        return '""'
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def dumps(cfg):
    L = ["# cpu-power-tuner. Править можно и руками, потом: cpt apply",
         "# enabled = false -> параметр не трогается (заводское значение).", "", "[general]"]
    L += [f"{k} = {_tv(v)}" for k, v in cfg["general"].items()]
    L += ["", "# какой пресет включать в каждом профиле power-profiles-daemon", "[bindings]"]
    L += [f"{k} = {_tv(v)}" for k, v in cfg["bindings"].items()]
    for pid, p in cfg["presets"].items():
        L += ["", f"[presets.{pid}]"]
        for k, v in p.items():
            if k in PARAMS:
                L.append(f"{k} = {_tv(v)}  # {PARAMS[k][4]}")
            elif v is not None:
                L.append(f"{k} = {_tv(v)}")
    return "\n".join(L) + "\n"


def save(cfg):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        f.write(dumps(cfg))
    os.replace(tmp, CONFIG_PATH)


def resolve_dynamic(preset, battery_pct, on_ac, current_static, hyst):
    """Какой статический пресет должен работать. current_static — что работает сейчас (для гистерезиса)."""
    if on_ac:
        return (preset.get("on_ac") or preset["default"]), "от сети"
    if battery_pct is None:
        return preset["default"], "нет батареи"
    steps = sorted(preset["steps"], key=lambda s: s["below"])  # от самого низкого порога

    def pick(pct):
        for i, s in enumerate(steps):
            if pct <= s["below"]:
                return i
        return len(steps)

    idx = pick(battery_pct)
    # гистерезис: при подзарядке не уходим со ступени, пока не зарядимся на hyst% выше её порога
    cur_idx = next((i for i, s in enumerate(steps) if s["preset"] == current_static), None)
    if cur_idx is not None and idx > cur_idx and battery_pct <= steps[cur_idx]["below"] + hyst:
        return steps[cur_idx]["preset"], f"заряд {battery_pct}%, держу ступень ≤ {steps[cur_idx]['below']}% (гистерезис {hyst}%)"
    if idx == len(steps):
        return preset["default"], f"заряд {battery_pct}% > всех порогов"
    return steps[idx]["preset"], f"заряд {battery_pct}% ≤ {steps[idx]['below']}%"
