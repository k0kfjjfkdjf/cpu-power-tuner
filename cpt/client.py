"""Клиент к службе + простой CLI: python3 -m cpt.client status|apply|config"""
import json
import socket
import sys

from .daemon import SOCKET_PATH


class DaemonError(Exception):
    pass


class Client:
    def __init__(self, path=SOCKET_PATH):
        self.path = path
        self.sock = None
        self.buf = b""

    def _connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(3)
        try:
            s.connect(self.path)
        except FileNotFoundError:
            raise DaemonError("служба не запущена (sudo systemctl start cpu-power-tuner)")
        except PermissionError:
            raise DaemonError("нет доступа к сокету: пользователь должен быть в группе из socket_group (обычно wheel)")
        except OSError as e:
            raise DaemonError(f"не удалось подключиться к службе: {e}")
        self.sock, self.buf = s, b""

    def request(self, **req):
        for attempt in (1, 2):
            try:
                if self.sock is None:
                    self._connect()
                self.sock.sendall((json.dumps(req) + "\n").encode())
                while b"\n" not in self.buf:
                    chunk = self.sock.recv(65536)
                    if not chunk:
                        raise ConnectionError("служба закрыла соединение")
                    self.buf += chunk
                line, self.buf = self.buf.split(b"\n", 1)
                resp = json.loads(line)
                if not resp.get("ok"):
                    raise DaemonError(resp.get("error", "ошибка"))
                return resp
            except (ConnectionError, OSError, json.JSONDecodeError) as e:
                self.close()
                if attempt == 2:
                    raise DaemonError(str(e))

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None


def _fmt(v, unit="", nd=0):
    if v is None:
        return "—"
    return f"{v:.{nd}f}{unit}" if isinstance(v, float) else f"{v}{unit}"


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    c = Client()
    try:
        if cmd == "status":
            import time
            c.request(cmd="status")
            time.sleep(1)
            s = c.request(cmd="status")["status"]
            ctl = s["controls"]
            print(f"профиль ppd: {s['profile']} -> пресет {s['bound_preset']} -> работает {s['static_preset']} ({s['reason']})")
            print(f"применено: {s['last_apply']}  {', '.join(s['last_changes'])}")
            print(f"пакет: {_fmt(s['pkg_power'], ' Вт', 2)}  {_fmt(s['pkg_temp'], ' °C')}  | "
                  f"средняя частота {_fmt(s['avg_mhz'], ' МГц')}  | iGPU {_fmt(s['gpu_mhz'], ' МГц')}")
            pl1, pl2 = ctl.get("pl1_uw"), ctl.get("pl2_uw")
            mf = sorted(set((ctl.get("max_freq_khz") or {}).values()))
            print(f"PL1/PL2: {pl1 / 1e6 if pl1 else '—'}/{pl2 / 1e6 if pl2 else '—'} Вт  | TCC offset {ctl.get('tcc_offset')}  | "
                  f"турбо {ctl.get('turbo')}  | max_freq {'/'.join(str((v or 0) // 1000) for v in mf)} МГц  | EPP {ctl.get('epp')}")
            if s["hot_cores"] or s["boost_off"]:
                print(f"умный троттлинг: горячие ядра {s['hot_cores']}  | буст выключен по t: {s['boost_off']}")
            print(f"{'поток':>5} {'ядро':>4} {'нагрузка':>9} {'частота':>9} {'t':>6}")
            for t in s["threads"]:
                print(f"{t['cpu']:>5} {t['core']:>4} {_fmt(t['load'], '%', 1):>9} {t['mhz']:>5} МГц {_fmt(t['temp'], ' °C'):>6}")
            for e in s["errors"]:
                print("ошибка:", e)
        elif cmd == "apply":
            r = c.request(cmd="apply")
            print("изменено:", ", ".join(r["changes"]) or "ничего", "| ошибки:", r["errors"] or "нет")
        elif cmd == "profile":
            prof = sys.argv[2] if len(sys.argv) > 2 else ""
            r = c.request(cmd="set_profile", profile=prof)
            print("профиль:", prof, "| изменено:", ", ".join(r["changes"]) or "ничего")
        elif cmd == "unpin":
            print("сброшено привязок:", c.request(cmd="unpin")["count"])
        elif cmd == "config":
            print(json.dumps(c.request(cmd="get_config")["config"], ensure_ascii=False, indent=1))
        else:
            print("использование: cpt status|apply|config|unpin|profile <power-saver|balanced|performance> (без ppd)")
    except DaemonError as e:
        raise SystemExit(f"ошибка: {e}")


if __name__ == "__main__":
    main()
