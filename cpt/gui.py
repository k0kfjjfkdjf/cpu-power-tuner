"""GUI в духе CoreCtrl: сверху метрики (список + график), снизу пресеты и система.

Работает от пользователя; железо трогает только служба (через unix-сокет).
"""
import copy
import sys
import time
from collections import deque

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from .client import Client, DaemonError  # noqa: E402

APP_ID = "local.cpupowertuner"
HISTORY = 120
ACCENT = "#ff6a2b"
PALETTE = ["#e5383b", "#8ae234", "#ff3df2", "#3d9bff", "#f6d32d", "#2ee6d6", "#ff8c1a", "#b57bff",
           "#57e389", "#99c1f1", "#ffbe6f", "#dc8add", "#62a0ea", "#f66151", "#c0bfbc", "#93e9be"]

CSS = f"""
window.cpt {{ background: #262626; color: #e8e8e8; }}
.panel {{ background: #2f2f2f; }}
.metrics-list {{ background: #2b2b2b; }}
.metrics-list row {{ padding: 1px 6px; min-height: 0; }}
.group {{ font-weight: 700; color: #9a9a9a; font-size: 9pt; margin-top: 6px; }}
.statusline {{ background: #2f2f2f; padding: 6px 12px; font-size: 9.5pt; }}
.card {{ background: #383838; border-radius: 6px; padding: 12px; }}
.card.running {{ box-shadow: inset 0 0 0 2px {ACCENT}; }}
.card-name {{ font-weight: 700; font-size: 12pt; }}
.dim {{ opacity: 0.62; }}
.small {{ font-size: 9pt; }}
.mono {{ font-family: monospace; }}
.err {{ color: #ff6b6b; }}
.hot {{ color: #ff6b6b; font-weight: 700; }}
.banner {{ background: #8c2a2a; color: white; padding: 8px; }}
.kind {{ font-size: 8pt; font-weight: 700; color: #bbb; border: 1px solid #666; border-radius: 3px; padding: 0 5px; }}
.badge-run {{ background: #2e9e5b; color: white; border-radius: 4px; padding: 1px 7px; font-size: 8.5pt; }}
.badge-bound {{ background: {ACCENT}; color: white; border-radius: 4px; padding: 1px 7px; font-size: 8.5pt; }}
.section {{ font-weight: 700; font-size: 11pt; }}
.param-title {{ font-weight: 600; }}
button.suggested-action {{ background: {ACCENT}; color: white; }}
.bottom-tabs {{ background: #333; }}
.bottom-tabs button {{ border-radius: 0; padding: 10px 60px; font-weight: 700; background: none; box-shadow: none; }}
.bottom-tabs button:checked {{ color: {ACCENT}; box-shadow: inset 0 3px {ACCENT}; background: #3a3a3a; }}
"""


def fmt(v, unit="", nd=0):
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{nd}f}{unit}"
    return f"{v}{unit}"


def margins(w, m):
    for side in ("top", "bottom", "start", "end"):
        getattr(w, f"set_margin_{side}")(m)
    return w


def label(text="", css=(), xalign=0.0, wrap=False, markup=False):
    lb = Gtk.Label(xalign=xalign, wrap=wrap)
    lb.set_markup(text) if markup else lb.set_label(text)
    for c in css:
        lb.add_css_class(c)
    return lb


def core_name(k):
    k = int(k)
    return f"п{k // 1000} ядро {k % 1000}" if k >= 1000 else f"ядро {k}"


def clear(box):
    while (c := box.get_first_child()) is not None:
        box.remove(c)


# =====================================================================  метрики
class Metric:
    def __init__(self, mid, group, name, unit, lo, hi, get, nd=0, checked=False):
        self.id, self.group, self.name, self.unit = mid, group, name, unit
        self.lo, self.hi, self.get, self.nd = lo, hi, get, nd
        self.checked = checked
        self.hist = deque(maxlen=HISTORY)
        self.color = "#ffffff"
        self.value = None
        self.minspan = {" °C": 6, " Вт": 2, " %": 10, " МГц": 400}.get(unit, (hi - lo) * 0.1)


class Graph(Gtk.DrawingArea):
    """Два режима: раздельные дорожки (у каждой метрики своя полоса и своя автошкала)
    и наложение (все линии в одном поле, каждая в своей фиксированной шкале)."""

    def __init__(self, metrics):
        super().__init__(hexpand=True, vexpand=True)
        self.metrics = metrics
        self.lanes = True
        self.smooth = True
        self.set_draw_func(self._draw)

    @staticmethod
    def _rgb(hexs):
        return tuple(int(hexs[i:i + 2], 16) / 255 for i in (1, 3, 5))

    @staticmethod
    def _ema(pts, a=0.35):
        out, acc = [], None
        for v in pts:
            if v is None:
                out.append(None)
                acc = None
                continue
            acc = v if acc is None else acc + a * (v - acc)
            out.append(acc)
        return out

    def _line(self, cr, m, pts, x0, w, ytop, ybot, lo, hi):
        step = w / (HISTORY - 1)
        off = HISTORY - len(pts)
        cr.set_source_rgb(*self._rgb(m.color))
        started = False
        for i, v in enumerate(pts):
            if v is None:
                started = False
                continue
            f = (min(max(v, lo), hi) - lo) / ((hi - lo) or 1)
            x, y = x0 + (i + off) * step, ybot - f * (ybot - ytop)
            cr.line_to(x, y) if started else cr.move_to(x, y)
            started = True
        cr.stroke()

    def _draw(self, _a, cr, w, h):
        cr.set_source_rgb(0.15, 0.15, 0.15)
        cr.paint()
        sel = [m for m in self.metrics if m.checked]
        cr.set_line_width(1)
        cr.set_source_rgba(1, 1, 1, 0.05)
        for i in range(1, 6):
            x = int(w * i / 6) + 0.5
            cr.move_to(x, 0)
            cr.line_to(x, h)
        cr.stroke()
        if not sel:
            return
        if self.lanes:
            n = len(sel)
            lh = h / n
            cr.select_font_face("monospace")
            for k, m in enumerate(sel):
                y0 = k * lh
                if k:
                    cr.set_source_rgba(1, 1, 1, 0.12)
                    cr.move_to(0, int(y0) + 0.5)
                    cr.line_to(w, int(y0) + 0.5)
                    cr.stroke()
                pts = list(m.hist)
                if self.smooth:
                    pts = self._ema(pts)
                vals = [v for v in pts if v is not None]
                if vals:
                    lo, hi = min(vals), max(vals)
                    span = max(hi - lo, m.minspan)
                    mid = (hi + lo) / 2
                    lo, hi = mid - span * 0.55, mid + span * 0.55
                    cr.set_line_width(1.6)
                    self._line(cr, m, pts, 0, w, y0 + 3, y0 + lh - 3, lo, hi)
                fs = 11 if lh >= 26 else max(8, lh * 0.55)
                cr.set_font_size(fs)
                txt = f"{m.name}: {fmt(m.value, m.unit, m.nd)}"
                ext = cr.text_extents(txt)
                cr.set_source_rgba(0.15, 0.15, 0.15, 0.75)
                cr.rectangle(4, y0 + 2, ext.x_advance + 8, fs + 4)
                cr.fill()
                cr.set_source_rgb(*self._rgb(m.color))
                cr.move_to(8, y0 + 2 + fs)
                cr.show_text(txt)
        else:
            cr.set_source_rgba(1, 1, 1, 0.06)
            for i in range(1, 4):
                y = int(h * i / 4) + 0.5
                cr.move_to(0, y)
                cr.line_to(w, y)
            cr.stroke()
            cr.set_line_width(1.6)
            for m in sel:
                pts = self._ema(list(m.hist)) if self.smooth else list(m.hist)
                self._line(cr, m, pts, 0, w, 6, h - 6, m.lo, m.hi)


class MetricsPanel(Gtk.Box):
    def __init__(self):
        super().__init__(spacing=0)
        self.add_css_class("panel")
        self.metrics = []
        self.rows = {}
        self.listbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        margins(self.listbox, 8)
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.add_css_class("metrics-list")
        sc.set_child(self.listbox)
        sc.set_size_request(380, -1)
        sc.set_hexpand(False)
        self.graph = Graph(self.metrics)
        self.append(sc)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        self.append(self.graph)
        self.set_size_request(-1, 220)

    def build(self, caps, factory, first_status):
        maxf = caps.get("max_freq_mhz") or 4000
        tj = caps.get("tjmax") or 100
        pmax = max(20, (factory.get("pl2_uw") or 20e6) / 1e6)
        M = []
        g = "Пакет"
        M.append(Metric("pkg_temp", g, "Температура", " °C", 30, tj, lambda s: s.get("pkg_temp"), checked=True))
        M.append(Metric("pkg_power", g, "Мощность", " Вт", 0, pmax, lambda s: s.get("pkg_power"), nd=1, checked=True))
        M.append(Metric("avg_load", g, "Использование", " %", 0, 100, lambda s: s.get("avg_load"), checked=True))
        M.append(Metric("avg_mhz", g, "Частота (средняя)", " МГц", 0, maxf, lambda s: s.get("avg_mhz"), checked=True))
        M.append(Metric("max_mhz", g, "Частота (макс.)", " МГц", 0, maxf, lambda s: s.get("max_mhz")))
        if caps.get("gpu"):
            M.append(Metric("gpu_mhz", g, "Встройка", " МГц", 0, 1300, lambda s: s.get("gpu_mhz")))
        M.append(Metric("battery", g, "Батарея", " %", 0, 100,
                        lambda s: ((s.get("power_supply") or {}).get("battery") or {}).get("capacity")))
        for core in sorted(first_status.get("core_temps", {}), key=int):
            M.append(Metric(f"core{core}", "Температура ядер", core_name(core).capitalize(), " °C", 30, tj,
                            lambda s, c=core: (s.get("core_temps") or {}).get(c)))
        for t in first_status.get("threads", []):
            cpu = t["cpu"]

            def getter(s, cpu=cpu):
                for x in s.get("threads", []):
                    if x["cpu"] == cpu:
                        return x.get("mhz")
            m = Metric(f"cpu{cpu}", "Потоки: частота · использование", f"CPU {cpu} ({core_name(t['core'])})",
                       " МГц", 0, maxf, getter)
            M.append(m)
        for i, m in enumerate(M):
            m.color = PALETTE[i % len(PALETTE)]
        self.metrics[:] = M

        clear(self.listbox)
        opts = Gtk.Box(spacing=10)
        for text, attr in (("раздельные дорожки", "lanes"), ("сглаживание", "smooth")):
            cb = Gtk.CheckButton(label=text, active=getattr(self.graph, attr))
            cb.add_css_class("small")
            cb.connect("toggled", lambda b, a=attr: (setattr(self.graph, a, b.get_active()), self.graph.queue_draw()))
            opts.append(cb)
        self.listbox.append(opts)
        group = None
        for m in M:
            if m.group != group:
                group = m.group
                self.listbox.append(label(group.upper(), ("group",)))
            row = Gtk.Box(spacing=6)
            cb = Gtk.CheckButton(active=m.checked)
            cb.connect("toggled", self._toggled, m)
            dot = label(f'<span foreground="{m.color}">■</span>', markup=True)
            name = label(m.name)
            name.set_hexpand(True)
            val = label("—", ("mono",), xalign=1)
            row.append(cb)
            row.append(dot)
            row.append(name)
            row.append(val)
            row.set_tooltip_text(f"в режиме наложения шкала {m.lo:g}–{m.hi:g}{m.unit}; на дорожке — автомасштаб")
            self.listbox.append(row)
            self.rows[m.id] = val

    def _toggled(self, cb, m):
        m.checked = cb.get_active()
        self.graph.queue_draw()

    def update(self, s):
        loads = {t["cpu"]: t.get("load") for t in s.get("threads", [])}
        hot = set(s.get("hot_cores") or [])
        for m in self.metrics:
            v = m.get(s)
            m.value = v
            m.hist.append(v)
            txt = fmt(v, m.unit, m.nd)
            if m.id.startswith("cpu"):
                txt = f"{fmt(loads.get(int(m.id[3:])), ' %', 0):>5}  {txt:>9}"
            if m.id.startswith("core") and int(m.id[4:]) in hot:
                txt = "▼ " + txt
                self.rows[m.id].add_css_class("hot")
            elif m.id.startswith("core"):
                self.rows[m.id].remove_css_class("hot")
            self.rows[m.id].set_label(txt)
        self.graph.queue_draw()


# =====================================================================  редактор параметров
class ParamRow:
    def __init__(self, grid, row, key, meta, ctx, on_change):
        _lo, _hi, step, title, hint = meta
        self.key, self.on_change, self.loading = key, on_change, False
        caps = ctx["caps"]
        self.sw = Gtk.Switch(valign=Gtk.Align.CENTER, tooltip_text="Управлять. Выкл — заводское значение.")
        tb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        tb.append(label(title, ("param-title",)))
        h = label(hint, ("small", "dim"), wrap=True)
        h.set_max_width_chars(55)
        tb.append(h)
        tb.set_hexpand(True)
        fac = label(f"заводское:\n{ctx['factory_text'].get(key, '—')}", ("small", "dim"))

        self.kind = "num"
        if key == "turbo":
            self.kind = "bool"
            self.ctrl = Gtk.Switch(valign=Gtk.Align.CENTER, halign=Gtk.Align.START)
            self.ctrl.connect("notify::active", self._changed)
        elif key == "epp":
            self.kind = "choice"
            self.choices = caps.get("epp_choices") or ["power"]
            self.ctrl = Gtk.DropDown.new_from_strings(self.choices)
            self.ctrl.set_valign(Gtk.Align.CENTER)
            self.ctrl.connect("notify::selected", self._changed)
        elif key in ctx["dict_fields"]:
            self.kind = "dict"
            self.ctrl = Gtk.Grid(column_spacing=8, row_spacing=4)
            self.spins = {}
            for i, (f, (flo, fhi, fstep, flabel)) in enumerate(ctx["dict_fields"][key].items()):
                if f == "core_mhz":
                    flo, fhi = caps.get("min_freq_mhz") or flo, caps.get("max_freq_mhz") or fhi
                sp = Gtk.SpinButton.new_with_range(flo, fhi, fstep)
                sp.set_digits(1 if fstep < 1 else 0)
                sp.connect("value-changed", self._changed)
                self.ctrl.attach(label(flabel, ("small",)), 0, i, 1, 1)
                self.ctrl.attach(sp, 1, i, 1, 1)
                self.spins[f] = sp
        else:
            lo, hi = meta[0], meta[1]
            if key in ("pl1_w", "pl2_w"):
                hi = min(hi, max(40, ctx["factory_pl2_w"]))
            elif key == "temp_limit_c":
                hi = caps.get("tjmax") or hi
                lo = max(lo, hi - (caps.get("tcc_max") or 60))
            elif key == "max_freq_mhz":
                lo, hi = caps.get("min_freq_mhz") or lo, caps.get("max_freq_mhz") or hi
            self.ctrl = Gtk.SpinButton.new_with_range(lo, hi, step)
            self.ctrl.set_digits(1 if step < 1 else 0)
            self.ctrl.set_valign(Gtk.Align.CENTER)
            self.ctrl.connect("value-changed", self._changed)
            self.is_float = step < 1
        self.sw.connect("notify::active", self._toggled)
        grid.attach(self.sw, 0, row, 1, 1)
        grid.attach(tb, 1, row, 1, 1)
        grid.attach(self.ctrl, 2, row, 1, 1)
        grid.attach(fac, 3, row, 1, 1)

    def _toggled(self, *_):
        self.ctrl.set_sensitive(self.sw.get_active())
        self._changed()

    def _changed(self, *_):
        if not self.loading:
            self.on_change()

    def set(self, item):
        self.loading = True
        self.sw.set_active(bool(item["enabled"]))
        v = item["value"]
        if self.kind == "num":
            self.ctrl.set_value(float(v))
        elif self.kind == "bool":
            self.ctrl.set_active(bool(v))
        elif self.kind == "choice":
            self.ctrl.set_selected(self.choices.index(v) if v in self.choices else 0)
        else:
            for f, sp in self.spins.items():
                sp.set_value(float(v.get(f, 0)))
        self.ctrl.set_sensitive(self.sw.get_active())
        self.loading = False

    def get(self):
        if self.kind == "num":
            v = self.ctrl.get_value()
            v = round(v * 2) / 2 if self.is_float else int(round(v))
        elif self.kind == "bool":
            v = self.ctrl.get_active()
        elif self.kind == "choice":
            v = self.choices[self.ctrl.get_selected()]
        else:
            v = {f: (sp.get_value() if sp.get_digits() else int(sp.get_value())) for f, sp in self.spins.items()}
        return {"enabled": self.sw.get_active(), "value": v}


# =====================================================================  пресеты
class PresetsPage(Gtk.Stack):
    def __init__(self, win):
        super().__init__(transition_type=Gtk.StackTransitionType.SLIDE_LEFT_RIGHT, vexpand=True)
        self.win = win
        self.cfg = None
        self.ctx = None
        self.status_info = {}
        self.editing = None

        # ----- список
        lst = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        margins(lst, 14)
        top = Gtk.Box(spacing=8)
        top.append(label("Пресеты", ("section",)))
        sp = Gtk.Box(hexpand=True)
        top.append(sp)
        b1 = Gtk.Button(label="+ Статический")
        b1.connect("clicked", lambda *_: self.new_preset("static"))
        b2 = Gtk.Button(label="+ Динамический (по батарее)")
        b2.connect("clicked", lambda *_: self.new_preset("dynamic"))
        top.append(b1)
        top.append(b2)
        lst.append(top)

        self.bind_box = Gtk.Box(spacing=12)
        self.bind_box.add_css_class("card")
        lst.append(self.bind_box)

        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                min_children_per_line=1, max_children_per_line=4,
                                column_spacing=10, row_spacing=10, valign=Gtk.Align.START)
        lst.append(self.flow)
        self.msg = label("", ("small", "dim"), wrap=True)
        lst.append(self.msg)
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        sc.set_child(lst)
        self.add_named(sc, "list")

        # ----- редактор
        self.editor = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add_named(self.editor, "edit")

    # ---------- данные ----------
    def setup(self, caps_resp):
        caps = caps_resp["caps"]
        fac = caps_resp["factory"]
        tj = caps.get("tjmax", 100)
        self.ctx = {
            "caps": caps, "params": caps_resp["params"], "dict_fields": caps_resp["dict_fields"],
            "templates": caps_resp["templates"], "ppd_profiles": caps_resp["ppd_profiles"],
            "ppd_titles": caps_resp["ppd_titles"], "factory_pl2_w": (fac.get("pl2_uw") or 40e6) / 1e6,
            "factory_text": {
                "pl1_w": f"{fac['pl1_uw'] / 1e6:g} Вт" if fac.get("pl1_uw") else "—",
                "pl2_w": f"{fac['pl2_uw'] / 1e6:g} Вт" if fac.get("pl2_uw") else "—",
                "temp_limit_c": f"{tj - fac['tcc_offset']} °C" if fac.get("tcc_offset") is not None else "—",
                "turbo": {True: "вкл", False: "выкл"}.get(fac.get("turbo"), "—"),
                "max_freq_mhz": f"{caps.get('max_freq_mhz')} МГц",
                "boost_temp_c": "не ограничен", "epp": "как ставит ppd", "smart": "выключен",
                "park": "выключена", "focus": "выключено",
            },
            "available": {"pl1_w": caps.get("rapl"), "pl2_w": caps.get("rapl"), "temp_limit_c": caps.get("tcc"),
                          "turbo": caps.get("turbo"), "max_freq_mhz": True, "boost_temp_c": caps.get("turbo"),
                          "epp": caps.get("epp"), "smart": caps.get("per_core_temps"), "park": caps.get("per_core_temps"),
                          "focus": True},
        }
        self.reload()

    def reload(self):
        try:
            self.cfg = self.win.client.request(cmd="get_config")["config"]
        except DaemonError as e:
            self.msg.set_label(f"Не удалось прочитать конфиг: {e}")
            return
        self.rebuild()

    def push(self, cfg, what):
        try:
            r = self.win.client.request(cmd="set_config", config=cfg)
        except DaemonError as e:
            self.msg.set_label(f"Ошибка: {e}")
            return False
        self.cfg = r["config"]
        ch = ", ".join(r.get("changes") or []) or "без изменений в железе"
        err = r.get("errors") or []
        self.msg.set_label(f"{what}. Применено: {ch}" + (f". Ошибки: {'; '.join(err)}" if err else ""))
        self.rebuild()
        return True

    def static_ids(self):
        return [k for k, v in self.cfg["presets"].items() if v["type"] == "static"]

    def name(self, pid):
        return self.cfg["presets"].get(pid, {}).get("name", pid or "—")

    # ---------- список ----------
    def rebuild(self):
        if not self.cfg or not self.ctx:
            return
        clear(self.bind_box)
        self.bind_box.append(label("Профиль power-profiles-daemon → пресет:", ("param-title",)))
        ids = list(self.cfg["presets"].keys())
        names = [self.name(i) for i in ids]
        for prof in self.ctx["ppd_profiles"]:
            b = Gtk.Box(spacing=6)
            b.append(label(self.ctx["ppd_titles"][prof]))
            dd = Gtk.DropDown.new_from_strings(names)
            cur = self.cfg["bindings"].get(prof)
            dd.set_selected(ids.index(cur) if cur in ids else 0)
            dd.connect("notify::selected", self._bind_changed, prof, ids)
            b.append(dd)
            self.bind_box.append(b)

        while (c := self.flow.get_child_at_index(0)) is not None:
            self.flow.remove(c)
        for pid, p in self.cfg["presets"].items():
            self.flow.append(self._card(pid, p))
        self.update_badges()

    def _bind_changed(self, dd, _p, prof, ids):
        pid = ids[dd.get_selected()]
        if self.cfg["bindings"].get(prof) == pid:
            return
        cfg = copy.deepcopy(self.cfg)
        cfg["bindings"][prof] = pid
        GLib.idle_add(lambda: self.push(cfg, f"Профиль «{self.ctx['ppd_titles'][prof]}» → «{self.name(pid)}»") and False)

    def summary(self, p):
        if p["type"] == "dynamic":
            parts = [f"≤{s['below']}% → {self.name(s['preset'])}" for s in sorted(p["steps"], key=lambda s: -s["below"])]
            parts.append(f"иначе → {self.name(p['default'])}")
            parts.append(f"от сети → {self.name(p['on_ac']) if p['on_ac'] else self.name(p['default'])}")
            return "\n".join(parts)
        bits = []
        if p["pl1_w"]["enabled"] or p["pl2_w"]["enabled"]:
            bits.append(f"PL {p['pl1_w']['value']:g}/{p['pl2_w']['value']:g} Вт" if p["pl1_w"]["enabled"] else f"PL2 {p['pl2_w']['value']:g} Вт")
        if p["temp_limit_c"]["enabled"]:
            bits.append(f"порог {p['temp_limit_c']['value']} °C")
        if p["turbo"]["enabled"]:
            bits.append("турбо " + ("вкл" if p["turbo"]["value"] else "выкл"))
        if p["max_freq_mhz"]["enabled"]:
            bits.append(f"≤{p['max_freq_mhz']['value']} МГц")
        if p["boost_temp_c"]["enabled"]:
            bits.append(f"буст до {p['boost_temp_c']['value']} °C")
        if p["epp"]["enabled"]:
            bits.append(f"EPP {p['epp']['value']}")
        if p["smart"]["enabled"]:
            sv = p["smart"]["value"]
            bits.append(f"умный: {sv['trigger_c']} °C → {sv['core_mhz']} МГц")
        if p.get("park", {}).get("enabled"):
            pv = p["park"]["value"]
            bits.append(f"парковка с {pv['trigger_c']} °C" + (" навсегда" if not pv["hold_s"] else ""))
        if p.get("focus", {}).get("enabled"):
            bits.append(f"активное окно → {p['focus']['value']['cores']} холодн. ядра")
        return " · ".join(bits) or "всё заводское"

    def _card(self, pid, p):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card.add_css_class("card")
        card.set_size_request(250, -1)
        head = Gtk.Box(spacing=6)
        head.append(label("ДИН" if p["type"] == "dynamic" else "СТАТ", ("kind",)))
        nm = label(p["name"], ("card-name",))
        nm.set_hexpand(True)
        nm.set_ellipsize(Pango.EllipsizeMode.END)
        head.append(nm)
        mb = Gtk.MenuButton(icon_name="view-more-symbolic")
        mb.add_css_class("flat")
        pop = Gtk.Popover()
        pb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        margins(pb, 4)

        def item(text, cb):
            b = Gtk.Button(label=text)
            b.add_css_class("flat")
            b.get_child().set_xalign(0)
            b.connect("clicked", lambda *_: (pop.popdown(), cb()))
            pb.append(b)
        item("Настроить", lambda: self.edit(pid))
        item("Копировать", lambda: self.duplicate(pid))
        if p.get("builtin"):
            item("Сбросить к стандартному", lambda: self.reset(pid))
        else:
            item("Удалить", lambda: self.delete(pid))
        pop.set_child(pb)
        mb.set_popover(pop)
        head.append(mb)
        card.append(head)
        sm = label(self.summary(p), ("small", "dim"), wrap=True)
        sm.set_vexpand(True)
        sm.set_valign(Gtk.Align.START)
        card.append(sm)
        badges = Gtk.Box(spacing=4)
        card.append(badges)
        btn = Gtk.Button(label="Настроить")
        btn.connect("clicked", lambda *_: self.edit(pid))
        card.append(btn)
        card._pid, card._badges = pid, badges
        return card

    def update_badges(self, status=None):
        if status is not None:
            self.status_info = status
        s = self.status_info
        i = 0
        while (fc := self.flow.get_child_at_index(i)) is not None:
            i += 1
            card = fc.get_child()
            clear(card._badges)
            pid = card._pid
            if s.get("static_preset") == pid:
                card.add_css_class("running")
                card._badges.append(label("работает", ("badge-run",)))
            else:
                card.remove_css_class("running")
            for prof, b in (self.cfg or {}).get("bindings", {}).items():
                if b == pid:
                    css = ("badge-bound",) if prof == s.get("profile") else ("kind",)
                    card._badges.append(label(self.ctx["ppd_titles"][prof], css))

    # ---------- операции ----------
    def _new_id(self):
        return f"user_{int(time.time() * 1000) % 10**10}"

    def new_preset(self, kind):
        cfg = copy.deepcopy(self.cfg)
        pid = self._new_id()
        if kind == "static":
            p = copy.deepcopy(self.ctx["templates"]["performance"])
            p["name"] = "Новый пресет"
        else:
            st = self.static_ids()
            p = {"name": "Новый динамический", "type": "dynamic", "on_ac": "",
                 "default": "balanced" if "balanced" in st else st[0], "steps": []}
        p["builtin"] = None
        cfg["presets"][pid] = p
        if self.push(cfg, f"Создан «{p['name']}»"):
            self.edit(pid)

    def duplicate(self, pid):
        cfg = copy.deepcopy(self.cfg)
        p = copy.deepcopy(cfg["presets"][pid])
        p["name"] = f"{p['name']} (копия)"
        p["builtin"] = None
        cfg["presets"][self._new_id()] = p
        self.push(cfg, f"Скопирован «{self.name(pid)}»")

    def reset(self, pid):
        cfg = copy.deepcopy(self.cfg)
        b = cfg["presets"][pid].get("builtin")
        cfg["presets"][pid] = copy.deepcopy(self.ctx["templates"][b])
        self.push(cfg, f"«{self.name(pid)}» сброшен к стандартному")

    def delete(self, pid):
        users = [self.ctx["ppd_titles"][k] for k, v in self.cfg["bindings"].items() if v == pid]
        users += [f"«{p['name']}»" for p in self.cfg["presets"].values()
                  if p["type"] == "dynamic" and (pid in (p["default"], p["on_ac"]) or any(s["preset"] == pid for s in p["steps"]))]
        if users:
            self.msg.set_label(f"Нельзя удалить «{self.name(pid)}»: он используется — {', '.join(users)}.")
            return
        cfg = copy.deepcopy(self.cfg)
        name = self.name(pid)
        del cfg["presets"][pid]
        self.push(cfg, f"Удалён «{name}»")

    # ---------- редактор ----------
    def edit(self, pid):
        self.editing = pid
        p = copy.deepcopy(self.cfg["presets"][pid])
        clear(self.editor)
        bar = Gtk.Box(spacing=8)
        margins(bar, 10)
        back = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Назад без сохранения")
        back.connect("clicked", lambda *_: self.set_visible_child_name("list"))
        bar.append(back)
        bar.append(label("ДИН" if p["type"] == "dynamic" else "СТАТ", ("kind",)))
        entry = Gtk.Entry(text=p["name"], width_chars=28)
        bar.append(entry)
        bar.append(Gtk.Box(hexpand=True))
        if p.get("builtin"):
            rb = Gtk.Button(label="Сбросить к стандартному")
            rb.connect("clicked", lambda *_: (self.reset(pid), self.edit(pid)))
            bar.append(rb)
        cancel = Gtk.Button(label="Отмена")
        cancel.connect("clicked", lambda *_: self.set_visible_child_name("list"))
        save = Gtk.Button(label="Сохранить и применить")
        save.add_css_class("suggested-action")
        bar.append(cancel)
        bar.append(save)
        self.editor.append(bar)
        self.editor.append(Gtk.Separator())

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        margins(body, 16)
        sc = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        sc.set_child(body)
        self.editor.append(sc)

        if p["type"] == "static":
            body.append(label("Выключенный переключатель слева — параметр не трогается (заводское значение).",
                              ("small", "dim")))
            grid = Gtk.Grid(column_spacing=16, row_spacing=16)
            grid.add_css_class("card")
            rows = {}
            r = 0
            for key, meta in self.ctx["params"].items():
                if not self.ctx["available"].get(key):
                    continue
                rows[key] = ParamRow(grid, r, key, meta, self.ctx, lambda: None)
                rows[key].set(p[key])
                r += 1
            body.append(grid)

            def collect():
                for k, row in rows.items():
                    p[k] = row.get()
                return p
        else:
            collect = self._dynamic_editor(body, p, pid)

        def do_save(*_):
            q = collect()
            q["name"] = entry.get_text().strip() or q["name"]
            cfg = copy.deepcopy(self.cfg)
            cfg["presets"][pid] = q
            if self.push(cfg, f"Сохранён «{q['name']}»"):
                self.set_visible_child_name("list")
        save.connect("clicked", do_save)
        self.set_visible_child_name("edit")

    def _dynamic_editor(self, body, p, pid):
        st = self.static_ids()
        names = [self.name(i) for i in st]
        body.append(label(
            "Динамический пресет переключает статические по заряду батареи. Действует ступень с самым низким "
            "порогом, под который попал заряд. При подзарядке ступень держится ещё на "
            f"{self.cfg['general'].get('hysteresis_pct', 2)} %, чтобы не дёргаться туда-сюда.",
            ("small", "dim"), wrap=True))
        g = Gtk.Grid(column_spacing=12, row_spacing=10)
        g.add_css_class("card")
        g.attach(label("От сети:", ("param-title",)), 0, 0, 1, 1)
        ac_names = ["(как «выше всех порогов»)"] + names
        dd_ac = Gtk.DropDown.new_from_strings(ac_names)
        dd_ac.set_selected(st.index(p["on_ac"]) + 1 if p["on_ac"] in st else 0)
        g.attach(dd_ac, 1, 0, 1, 1)
        g.attach(label("На батарее выше всех порогов:", ("param-title",)), 0, 1, 1, 1)
        dd_def = Gtk.DropDown.new_from_strings(names)
        dd_def.set_selected(st.index(p["default"]) if p["default"] in st else 0)
        g.attach(dd_def, 1, 1, 1, 1)
        body.append(g)

        body.append(label("Ступени", ("section",)))
        steps_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        steps_box.add_css_class("card")
        body.append(steps_box)
        step_rows = []

        def add_row(below, preset):
            r = Gtk.Box(spacing=8)
            r.append(label("Когда заряд ≤"))
            sp = Gtk.SpinButton.new_with_range(1, 100, 1)
            sp.set_value(below)
            r.append(sp)
            r.append(label("%  →  включить"))
            dd = Gtk.DropDown.new_from_strings(names)
            dd.set_selected(st.index(preset) if preset in st else 0)
            dd.set_hexpand(True)
            r.append(dd)
            rm = Gtk.Button(icon_name="window-close-symbolic", tooltip_text="Удалить ступень")
            rm.add_css_class("flat")
            entry = (r, sp, dd)
            rm.connect("clicked", lambda *_: (steps_box.remove(r), step_rows.remove(entry)))
            r.append(rm)
            steps_box.append(r)
            step_rows.append(entry)

        for s in sorted(p["steps"], key=lambda s: -s["below"]):
            add_row(s["below"], s["preset"])
        addb = Gtk.Button(label="+ ступень")
        addb.set_halign(Gtk.Align.START)
        addb.connect("clicked", lambda *_: add_row(max([int(x[1].get_value()) for x in step_rows] + [30]) - 10 if step_rows else 20,
                                                   st[0]))
        body.append(addb)

        def collect():
            p["on_ac"] = "" if dd_ac.get_selected() == 0 else st[dd_ac.get_selected() - 1]
            p["default"] = st[dd_def.get_selected()]
            p["steps"] = [{"below": int(sp.get_value()), "preset": st[dd.get_selected()]} for _r, sp, dd in step_rows]
            return p
        return collect


# =====================================================================  система
class SystemPage(Gtk.ScrolledWindow):
    def __init__(self, win):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
        self.win = win
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        margins(box, 14)
        self.set_child(box)
        self.info = label("", wrap=True, markup=True)
        self.info.set_selectable(True)
        c1 = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        c1.add_css_class("card")
        c1.append(label("Железо", ("section",)))
        c1.append(self.info)
        box.append(c1)
        c2 = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        c2.add_css_class("card")
        c2.append(label("Служба: последние события", ("section",)))
        self.events = label("", ("mono", "small"), wrap=True)
        self.events.set_selectable(True)
        c2.append(self.events)
        box.append(c2)
        btns = Gtk.Box(spacing=8)
        b1 = Gtk.Button(label="Применить заново")
        b1.connect("clicked", self.reapply)
        self.b2 = Gtk.Button(label="Сбросить все пресеты к стандартным")
        self.b2.connect("clicked", self.reset_all)
        self.confirm_until = 0
        btns.append(b1)
        btns.append(self.b2)
        box.append(btns)
        self.msg = label("", ("small", "dim"), wrap=True)
        box.append(self.msg)

    def setup(self, caps_resp):
        c, f = caps_resp["caps"], caps_resp["factory"]

        def w(k):
            return f"{f[k] / 1e6:g} Вт" if f.get(k) else "—"
        yes = lambda b: "есть" if b else "<span foreground='#ff6b6b'>нет</span>"  # noqa: E731
        self.info.set_markup(
            f"<b>{GLib.markup_escape_text(c['model'])}</b>\n"
            f"{c['cores']} ядер / {c['threads']} потоков · {c['min_freq_mhz']}–{c['max_freq_mhz']} МГц, база {c['base_freq_mhz']} МГц · Tjmax {c['tjmax']} °C\n"
            f"RAPL (MSR): {yes(c['rapl'])} · RAPL прошивки (MMIO): {yes(c['rapl_mmio'])} · TCC offset: {yes(c['tcc'])} (до {c['tcc_max']} °C)\n"
            f"Турбо: {yes(c['turbo'])}{' (' + c['turbo_kind'] + ')' if c['turbo_kind'] else ''} · "
            f"EPP: {', '.join(c['epp_choices']) or 'нет'} · встройка: {yes(c['gpu'])}{' (' + c['gpu_kind'] + ')' if c['gpu_kind'] else ''}\n"
            f"Драйвер частоты: {c['driver']} · процессоров: {c['packages']} · гибридный (P/E): {'да' if c['hybrid'] else 'нет'}\n"
            f"Температуры: {GLib.markup_escape_text(c['temp_source'])}"
            + ("" if c["per_core_temps"] else " — <span foreground='#ffbe6f'>датчиков по ядрам нет: умный троттлинг и парковка недоступны</span>")
            + "\n\n"
            f"<b>Заводские значения</b> (сняты {f.get('captured', '?')}): PL1 {w('pl1_uw')}, PL2 {w('pl2_uw')}, "
            f"порог {c['tjmax'] - f['tcc_offset'] if 'tcc_offset' in f else '—'} °C, "
            f"турбо {'вкл' if f.get('turbo', True) else 'выкл'}")

    def update(self, s):
        self.events.set_label("\n".join(reversed(s.get("events") or [])) or "—")

    def reapply(self, *_):
        try:
            r = self.win.client.request(cmd="apply")
            self.msg.set_label("Применено: " + (", ".join(r["changes"]) or "всё уже стояло как надо"))
        except DaemonError as e:
            self.msg.set_label(str(e))

    def reset_all(self, *_):
        if time.monotonic() > self.confirm_until:
            self.confirm_until = time.monotonic() + 4
            self.b2.set_label("Точно? Нажмите ещё раз")
            GLib.timeout_add(4000, lambda: self.b2.set_label("Сбросить все пресеты к стандартным") and False)
            return
        self.confirm_until = 0
        self.b2.set_label("Сбросить все пресеты к стандартным")
        try:
            self.win.client.request(cmd="reset_all")
            self.msg.set_label("Все пресеты и привязки сброшены к стандартным.")
            self.win.presets.reload()
        except DaemonError as e:
            self.msg.set_label(str(e))


# =====================================================================  окно
class Window(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="CPU Power Tuner")
        self.add_css_class("cpt")
        import os
        w, h = (os.environ.get("CPT_GUI_SIZE") or "1280x880").split("x")
        self.set_default_size(int(w), int(h))
        self.client = Client()
        self.caps = None
        self.factory = {}
        self.profile = None
        self.manual_profile = False
        self._switching = False

        hb = Gtk.HeaderBar()
        self.title = label("CPU Power Tuner", ("param-title",))
        hb.set_title_widget(self.title)
        pbox = Gtk.Box()
        pbox.add_css_class("linked")
        self.prof_btns = {}
        first = None
        for p, t in (("power-saver", "Экономия"), ("balanced", "Баланс"), ("performance", "Производительность")):
            b = Gtk.ToggleButton(label=t, tooltip_text=f"powerprofilesctl set {p}")
            if first:
                b.set_group(first)
            else:
                first = b
            b.connect("toggled", self._profile_clicked, p)
            self.prof_btns[p] = b
            pbox.append(b)
        hb.pack_end(pbox)
        self.ppd_label = label("профиль ppd:", ("dim", "small"))
        hb.pack_start(self.ppd_label)
        self.set_titlebar(hb)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.banner = label("", ("banner",), wrap=True)
        self.banner.set_visible(False)
        root.append(self.banner)
        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.metrics = MetricsPanel()
        self.metrics.set_vexpand(True)
        top.append(self.metrics)
        self.statusline = label("", ("statusline",), wrap=True, markup=True)
        top.append(self.statusline)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        self.presets = PresetsPage(self)
        self.system = SystemPage(self)
        self.stack.add_titled(self.presets, "presets", "ПРЕСЕТЫ")
        self.stack.add_titled(self.system, "system", "СИСТЕМА")
        # разделитель можно тянуть мышью, как в CoreCtrl
        paned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, vexpand=True,
                          shrink_start_child=False, shrink_end_child=False)
        paned.set_start_child(top)
        paned.set_end_child(self.stack)
        paned.set_position(380)
        root.append(paned)
        tabs = Gtk.StackSwitcher(stack=self.stack, halign=Gtk.Align.FILL)
        tabs.add_css_class("bottom-tabs")
        tabs.set_homogeneous(True) if hasattr(tabs, "set_homogeneous") else None
        root.append(tabs)
        self.set_child(root)

        self._tick()
        GLib.timeout_add(1000, self._tick)

    def _init(self):
        resp = self.client.request(cmd="caps")
        self.caps, self.factory = resp["caps"], resp["factory"]
        first = self.client.request(cmd="status")["status"]
        self.metrics.build(self.caps, self.factory, first)
        self.presets.setup(resp)
        self.system.setup(resp)
        self.title.set_label(f"CPU Power Tuner — {self.caps['model']}")

    def _tick(self):
        try:
            if self.caps is None:
                self._init()
            s = self.client.request(cmd="status")["status"]
        except DaemonError as e:
            self.banner.set_label(f"Нет связи со службой: {e}")
            self.banner.set_visible(True)
            return True
        self.banner.set_visible(False)
        self.metrics.update(s)
        self.system.update(s)
        self._statusline(s)
        self.presets.update_badges(s)
        self.manual_profile = bool(s.get("manual_profile"))
        self.ppd_label.set_label("профиль (вручную, без ppd):" if self.manual_profile else "профиль ppd:")
        prof = s.get("profile")
        if prof != self.profile:
            self.profile = prof
            if prof in self.prof_btns:
                self._switching = True
                self.prof_btns[prof].set_active(True)
                self._switching = False
        return True

    def _statusline(self, s):
        cfg = self.presets.cfg or {"presets": {}}
        nm = lambda pid: GLib.markup_escape_text(cfg["presets"].get(pid, {}).get("name", str(pid)))  # noqa: E731
        ctl = s.get("controls", {})
        tj = self.caps.get("tjmax", 100)
        pl = lambda k: f"{ctl[k] / 1e6:g}" if ctl.get(k) else "—"  # noqa: E731
        mf = sorted({v // 1000 for v in (ctl.get("max_freq_khz") or {}).values() if v})
        chain = f"<b>{nm(s.get('bound_preset'))}</b>"
        if s.get("bound_preset") != s.get("static_preset"):
            chain += f" → работает <b>{nm(s.get('static_preset'))}</b>"
        parts = [
            f"Пресет: {chain} <span alpha='65%'>({GLib.markup_escape_text(s.get('reason') or '')})</span>",
            (f"PL1 {pl('pl1_uw')} / PL2 {pl('pl2_uw')} Вт"
             + (f" <span alpha='65%'>(прошивка {pl('mmio_pl1_uw')})</span>" if ctl.get("mmio_pl1_uw") else ""))
            if ctl.get("pl1_uw") else "",
            f"порог {tj - ctl['tcc_offset']} °C" if ctl.get("tcc_offset") is not None else "",
            ("турбо " + ("вкл" if ctl["turbo"] else "выкл")) if ctl.get("turbo") is not None else "",
            "потолок " + "/".join(map(str, mf)) + " МГц" if mf else "",
            f"EPP {ctl.get('epp')}" if ctl.get("epp") else "",
        ]
        if s.get("grace"):
            parts.append("<span foreground='#f6d32d'>остужаю парковкой: ядра " + ", ".join(s["grace"]) + "</span>")
        if s.get("hot_cores"):
            parts.append(f"<span foreground='#ff6b6b'><b>остывают ядра: {', '.join(map(str, s['hot_cores']))}</b></span>")
        fo = s.get("focus") or {}
        if fo.get("cores"):
            parts.append(f"<span foreground='#57e389'>в фокусе: {GLib.markup_escape_text(fo['name'])} → ядра "
                         f"{','.join(map(str, fo['cores']))}</span>")
        elif fo.get("note"):
            parts.append(f"<span alpha='65%'>активное окно: {GLib.markup_escape_text(fo['note'])}</span>")
        if s.get("parked"):
            pk = ", ".join(f"{GLib.markup_escape_text(x['comm'])} я{x['from']}→я{x['to']} "
                           f"({'∞' if x['left'] is None else str(x['left']) + ' с'})" for x in s["parked"])
            parts.append(f"<span foreground='#2ee6d6'>припаркованы: {pk}</span>")
        if s.get("boost_off"):
            parts.append("<span foreground='#ff6b6b'><b>буст выключен по температуре</b></span>")
        if s.get("errors"):
            parts.append(f"<span foreground='#ff6b6b'>ошибки: {GLib.markup_escape_text('; '.join(s['errors']))}</span>")
        self.statusline.set_markup("  ·  ".join(p for p in parts if p))

    def _profile_clicked(self, btn, prof):
        if self._switching or not btn.get_active() or prof == self.profile:
            return
        if self.manual_profile:  # power-profiles-daemon нет — профиль хранит служба
            try:
                self.client.request(cmd="set_profile", profile=prof)
            except DaemonError as e:
                self.banner.set_label(f"Не удалось переключить профиль: {e}")
                self.banner.set_visible(True)
            return
        try:
            Gio.Subprocess.new(["powerprofilesctl", "set", prof], Gio.SubprocessFlags.NONE)
        except GLib.Error as e:
            self.banner.set_label(f"Не удалось переключить профиль: {e.message}")
            self.banner.set_visible(True)


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_startup(self):
        Gtk.Application.do_startup(self)
        st = Gtk.Settings.get_default()
        if st is not None:
            st.set_property("gtk-application-prefer-dark-theme", True)
        prov = Gtk.CssProvider()
        if hasattr(prov, "load_from_string"):
            prov.load_from_string(CSS)
        else:
            prov.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_activate(self):
        win = self.props.active_window or Window(self)
        win.present()
        tab = next((a[6:] for a in sys.argv if a.startswith("--tab=")), None)
        if tab:
            win.stack.set_visible_child_name(tab)
        edit = next((a[7:] for a in sys.argv if a.startswith("--edit=")), None)
        if edit and win.presets.cfg and edit in win.presets.cfg["presets"]:
            win.presets.edit(edit)


def main():
    sys.exit(App().run(sys.argv[:1]))


if __name__ == "__main__":
    main()
