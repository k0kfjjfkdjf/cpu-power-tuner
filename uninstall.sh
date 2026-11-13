#!/usr/bin/env bash
# Удаление: sudo ./uninstall.sh [--purge]   (--purge удаляет и конфиг с заводскими значениями)
set -u
[ "$EUID" -eq 0 ] || { echo "Запустите через sudo: sudo $0"; exit 1; }

# при остановке служба сама возвращает заводские значения (restore_on_stop)
systemctl disable --now cpu-power-tuner.service 2>/dev/null
rm -f /etc/systemd/system/cpu-power-tuner.service /usr/share/applications/cpu-power-tuner.desktop \
      /usr/local/bin/cpu-power-tuner /usr/local/bin/cpt
rm -rf /opt/cpu-power-tuner
systemctl daemon-reload
if [ "${1:-}" = "--purge" ]; then
  rm -rf /etc/cpu-power-tuner /var/lib/cpu-power-tuner
  echo "Удалено вместе с конфигом."
else
  echo "Удалено. Конфиг оставлен: /etc/cpu-power-tuner, /var/lib/cpu-power-tuner"
fi
