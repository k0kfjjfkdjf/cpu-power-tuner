#!/usr/bin/env bash
# Установка / обновление cpu-power-tuner:  sudo ./install.sh
# Install / update:                         sudo ./install.sh
set -euo pipefail
[ "$EUID" -eq 0 ] || { echo "Запустите через sudo / run with sudo: sudo $0"; exit 1; }

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST=/opt/cpu-power-tuner
USER_NAME="${SUDO_USER:-}"

echo "==> Зависимости / dependencies"
if command -v pacman >/dev/null; then
  pacman -S --needed --noconfirm python python-gobject python-cairo gtk4 || true
  pacman -S --needed --noconfirm power-profiles-daemon || echo "   power-profiles-daemon не поставлен — профили будут переключаться вручную"
elif command -v apt-get >/dev/null; then
  apt-get install -y python3 python3-gi python3-gi-cairo gir1.2-gtk-4.0 || true
  apt-get install -y power-profiles-daemon || echo "   power-profiles-daemon не поставлен — профили будут переключаться вручную"
elif command -v dnf >/dev/null; then
  dnf install -y python3 python3-gobject python3-cairo gtk4 || true
  dnf install -y power-profiles-daemon || echo "   power-profiles-daemon не поставлен — профили будут переключаться вручную"
elif command -v zypper >/dev/null; then
  zypper --non-interactive install python3 python3-gobject python3-gobject-cairo typelib-1_0-Gtk-4_0 || true
  zypper --non-interactive install power-profiles-daemon || true
else
  echo "   неизвестный пакетный менеджер: поставьте вручную Python 3.11+, PyGObject, pycairo, GTK 4"
fi

python3 - <<'PY' || { echo "!! нужен Python 3.11+ (tomllib) / Python 3.11+ required"; exit 1; }
import sys, tomllib  # noqa
assert sys.version_info >= (3, 11)
PY

echo "==> Конфликты / conflicts"
for u in ppd-limits.path ppd-limits.service tlp auto-cpufreq thermald; do
  if systemctl is-enabled "$u" &>/dev/null || systemctl is-active "$u" &>/dev/null; then
    echo "   !! включена служба $u — она будет спорить с cpu-power-tuner за те же настройки."
    read -rp "      Отключить $u? / disable $u? [Y/n] " a || a=y
    [[ "${a:-y}" =~ ^[YyДд]?$ ]] && systemctl disable --now "$u" || true
  fi
done

echo "==> Файлы / files: $DEST"
mkdir -p "$DEST/cpt"
install -m 644 "$SRC"/cpt/*.py "$DEST/cpt/"

cat > /usr/local/bin/cpu-power-tuner <<EOF
#!/bin/sh
PYTHONPATH=$DEST exec python3 -m cpt.gui "\$@"
EOF
cat > /usr/local/bin/cpt <<EOF
#!/bin/sh
PYTHONPATH=$DEST exec python3 -m cpt.client "\$@"
EOF
chmod 755 /usr/local/bin/cpu-power-tuner /usr/local/bin/cpt

install -m 644 "$SRC/data/cpu-power-tuner.service" /etc/systemd/system/
install -m 644 "$SRC/data/cpu-power-tuner.desktop" /usr/share/applications/

if [ ! -f /var/lib/cpu-power-tuner/factory.json ]; then
  echo "==> Заводские значения будут сняты при первом запуске службы с ТЕКУЩИХ настроек."
  echo "    Factory values are captured from the CURRENT settings on first start."
  echo "    Если что-то уже менялось вручную — перезагрузитесь перед установкой. / Reboot first if you changed anything."
fi

echo "==> Служба / service"
systemctl daemon-reload
systemctl enable cpu-power-tuner.service
systemctl restart cpu-power-tuner.service   # при обновлении подхватить новую версию
sleep 1
systemctl --no-pager --lines=8 status cpu-power-tuner.service || true

if [ -n "$USER_NAME" ]; then
  grp=$(python3 -c "import tomllib;print(tomllib.load(open('/etc/cpu-power-tuner/config.toml','rb'))['general']['socket_group'])" 2>/dev/null || echo wheel)
  if ! getent group "$grp" >/dev/null; then
    echo "   !! группы $grp нет — служба сама выберет wheel/sudo/admin, что найдётся. / group missing, service falls back to wheel/sudo/admin"
  elif ! id -nG "$USER_NAME" | tr ' ' '\n' | grep -qx "$grp"; then
    echo "   !! $USER_NAME не в группе $grp — GUI не сможет управлять службой. / not in group $grp"
    echo "      sudo usermod -aG $grp $USER_NAME  (и перелогиниться / then re-login)"
  fi
fi

echo
echo "Готово / done. GUI: cpu-power-tuner · CLI: cpt status · логи / logs: journalctl -u cpu-power-tuner -f"
