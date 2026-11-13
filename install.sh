#!/usr/bin/env bash
# Установка cpu-power-tuner: sudo ./install.sh
set -euo pipefail
[ "$EUID" -eq 0 ] || { echo "Запустите через sudo: sudo $0"; exit 1; }

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST=/opt/cpu-power-tuner
USER_NAME="${SUDO_USER:-}"

echo "==> Зависимости"
pacman -S --needed --noconfirm python python-gobject python-cairo gtk4 power-profiles-daemon

echo "==> Конфликты"
for u in ppd-limits.path ppd-limits.service tlp auto-cpufreq thermald; do
  if systemctl is-enabled "$u" &>/dev/null || systemctl is-active "$u" &>/dev/null; then
    echo "   !! включена служба $u — она будет спорить с cpu-power-tuner за те же настройки."
    read -rp "      Отключить $u? [Y/n] " a
    [[ "${a:-y}" =~ ^[YyДд]?$ ]] && systemctl disable --now "$u" || true
  fi
done

echo "==> Файлы в $DEST"
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
  echo "    Сейчас: PL1=$(cat /sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw 2>/dev/null) PL2=$(cat /sys/class/powercap/intel-rapl:0/constraint_1_power_limit_uw 2>/dev/null)" \
       "no_turbo=$(cat /sys/devices/system/cpu/intel_pstate/no_turbo 2>/dev/null) max_perf=$(cat /sys/devices/system/cpu/intel_pstate/max_perf_pct 2>/dev/null)"
  echo "    Если что-то из этого уже изменено вручную — перезагрузитесь перед установкой."
fi

echo "==> Служба"
systemctl daemon-reload
systemctl enable cpu-power-tuner.service
systemctl restart cpu-power-tuner.service   # при обновлении подхватить новую версию
sleep 1
systemctl --no-pager --lines=8 status cpu-power-tuner.service || true

if [ -n "$USER_NAME" ]; then
  grp=$(python3 -c "import tomllib;print(tomllib.load(open('/etc/cpu-power-tuner/config.toml','rb'))['general']['socket_group'])" 2>/dev/null || echo wheel)
  if ! id -nG "$USER_NAME" | tr ' ' '\n' | grep -qx "$grp"; then
    echo "   !! $USER_NAME не в группе $grp — GUI не сможет управлять службой."
    echo "      sudo usermod -aG $grp $USER_NAME  (и перелогиниться)"
  fi
fi

echo
echo "Готово. GUI: cpu-power-tuner (или в меню приложений), консоль: cpt status"
echo "Логи службы: journalctl -u cpu-power-tuner -f"
