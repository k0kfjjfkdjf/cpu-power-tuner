#!/usr/bin/env bash
# probe.sh — разведка: что из управления питанием CPU/iGPU реально доступно на этой машине.
# ТОЛЬКО ЧИТАЕТ. Ничего не меняет (кроме загрузки модуля ядра msr для чтения регистров).
# Запуск: sudo ./probe.sh
# Результат: ../results/probe-<hostname>-<дата>.txt

set -u
if [ "$EUID" -ne 0 ]; then
  echo "Запустите через sudo: sudo $0"; exit 1
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
OUTDIR="$HERE/../results"
mkdir -p "$OUTDIR"
HOST="$(cat /etc/hostname 2>/dev/null || uname -n)"
OUT="$OUTDIR/probe-$HOST-$(date +%Y%m%d-%H%M%S).txt"

exec > >(tee "$OUT") 2>&1

sec() { printf '\n===== %s =====\n' "$*"; }
show() { # show <файл...>: печатает "путь = значение" для читаемых файлов
  for f in "$@"; do
    [ -e "$f" ] || continue
    if [ -r "$f" ] && [ -f "$f" ]; then
      v=$(cat "$f" 2>&1 | tr '\n' ' ')
      w=" [$(stat -c %A "$f")]"
      printf '%s = %s%s\n' "$f" "$v" "$w"
    fi
  done
}

sec "Система"
date
uname -a
echo "host: $HOST"
grep -m1 "model name" /proc/cpuinfo
grep -m1 "cpu family" /proc/cpuinfo; grep -m1 "^model\s" /proc/cpuinfo; grep -m1 stepping /proc/cpuinfo
grep -m1 -o -w -E 'hwp|hwp_epp|ida|intel_pt' /proc/cpuinfo | sort -u | tr '\n' ' '; echo
nproc
cat /proc/cmdline
cat /sys/class/dmi/id/sys_vendor /sys/class/dmi/id/product_name /sys/class/dmi/id/bios_version /sys/class/dmi/id/bios_date 2>/dev/null

sec "intel_pstate (глобально)"
show /sys/devices/system/cpu/intel_pstate/*

sec "cpufreq по потокам"
for c in /sys/devices/system/cpu/cpu[0-9]*; do
  echo "--- $(basename "$c")"
  show "$c"/cpufreq/{scaling_driver,scaling_governor,scaling_available_governors,energy_performance_preference,energy_performance_available_preferences,cpuinfo_min_freq,cpuinfo_max_freq,base_frequency,scaling_min_freq,scaling_max_freq,scaling_cur_freq}
  show "$c"/power/energy_perf_bias
done

sec "Powercap / RAPL"
for z in /sys/class/powercap/*; do
  [ -d "$z" ] || continue
  echo "--- $z"
  show "$z"/{name,enabled,max_energy_range_uj}
  show "$z"/constraint_*_{name,power_limit_uw,time_window_us,max_power_uw,min_power_uw}
done

sec "Потребление по зонам RAPL (замер 5 с)"
declare -A E1
for z in /sys/class/powercap/intel-rapl*:*; do
  [ -r "$z/energy_uj" ] && E1[$z]=$(cat "$z/energy_uj")
done
sleep 5
for z in "${!E1[@]}"; do
  e2=$(cat "$z/energy_uj")
  d=$(( e2 - ${E1[$z]} ))
  [ $d -lt 0 ] && d=0
  printf '%s (%s): %d.%02d W\n' "$z" "$(cat "$z/name")" $((d/5000000)) $(( (d/50000) % 100 ))
done

sec "MSR (регистры процессора)"
modprobe msr 2>/dev/null
python3 - <<'PY'
import os, struct
def rd(reg, cpu=0):
    try:
        fd = os.open(f"/dev/cpu/{cpu}/msr", os.O_RDONLY)
        try: return struct.unpack("<Q", os.pread(fd, 8, reg))[0]
        finally: os.close(fd)
    except Exception as e:
        return e
def bits(v, hi, lo): return (v >> lo) & ((1 << (hi - lo + 1)) - 1)

v = rd(0xCE)  # MSR_PLATFORM_INFO
if isinstance(v, int):
    print(f"PLATFORM_INFO 0xCE = {v:#x}")
    print(f"  базовый множитель (bits 15:8) = {bits(v,15,8)}  -> ~{bits(v,15,8)*100} МГц")
    print(f"  программируемый TCC offset (bit 30) = {bits(v,30,30)}")
    print(f"  программируемые лимиты турбо (bit 28) = {bits(v,28,28)}")
else: print("PLATFORM_INFO: ошибка", v)

v = rd(0x1A2)  # MSR_IA32_TEMPERATURE_TARGET
if isinstance(v, int):
    print(f"TEMPERATURE_TARGET 0x1A2 = {v:#x}")
    print(f"  Tjmax (bits 23:16) = {bits(v,23,16)} °C")
    print(f"  текущий TCC offset (bits 29:24) = {bits(v,29,24)} °C")
    print(f"  блокировка (bit 31) = {bits(v,31,31)}")
else: print("TEMPERATURE_TARGET: ошибка", v)

v = rd(0x606)  # MSR_RAPL_POWER_UNIT
pu = 1.0
if isinstance(v, int):
    pu = 1 / (1 << bits(v,3,0))
    print(f"RAPL_POWER_UNIT 0x606 = {v:#x}  (единица мощности {pu} Вт)")

v = rd(0x610)  # MSR_PKG_POWER_LIMIT
if isinstance(v, int):
    print(f"PKG_POWER_LIMIT 0x610 = {v:#x}")
    print(f"  PL1 = {bits(v,14,0)*pu:.2f} Вт, вкл={bits(v,15,15)}, clamp={bits(v,16,16)}")
    print(f"  PL2 = {bits(v,46,32)*pu:.2f} Вт, вкл={bits(v,47,47)}, clamp={bits(v,48,48)}")
    print(f"  БЛОКИРОВКА (bit 63) = {bits(v,63,63)}  <- 1 значит BIOS запретил менять лимиты через MSR")
else: print("PKG_POWER_LIMIT: ошибка", v)

v = rd(0x614)  # MSR_PKG_POWER_INFO
if isinstance(v, int):
    print(f"PKG_POWER_INFO 0x614: TDP = {bits(v,14,0)*pu:.2f} Вт")

v = rd(0x1AD)  # MSR_TURBO_RATIO_LIMIT
if isinstance(v, int):
    print("TURBO_RATIO_LIMIT 0x1AD: " + ", ".join(f"{i+1}яд={bits(v,8*i+7,8*i)*100}МГц" for i in range(4)))

v = rd(0x1B1)  # IA32_PACKAGE_THERM_STATUS
if isinstance(v, int):
    print(f"PACKAGE_THERM_STATUS: до Tjmax осталось {bits(v,22,16)} °C, лог троттлинга по t={bits(v,1,1)}, по мощности={bits(v,11,11)}")

v = rd(0x64F)  # MSR_CORE_PERF_LIMIT_REASONS
if isinstance(v, int):
    print(f"CORE_PERF_LIMIT_REASONS 0x64F = {v:#x} (причины ограничения частоты, лог-биты 31:16)")
PY

sec "Термозоны"
for t in /sys/class/thermal/thermal_zone*; do
  printf '%s: type=%s temp=%s policy=%s\n' "$(basename "$t")" "$(cat "$t/type")" "$(cat "$t/temp" 2>/dev/null)" "$(cat "$t/policy" 2>/dev/null)"
  for tp in "$t"/trip_point_*_temp; do
    [ -e "$tp" ] && printf '    %s = %s (%s)\n' "$(basename "$tp")" "$(cat "$tp")" "$(cat "${tp%_temp}_type" 2>/dev/null)"
  done
done

sec "Устройства охлаждения"
for c in /sys/class/thermal/cooling_device*; do
  w=" [$(stat -c %A "$c/cur_state")]"
  printf '%s: type=%s cur=%s max=%s%s\n' "$(basename "$c")" "$(cat "$c/type")" "$(cat "$c/cur_state" 2>/dev/null)" "$(cat "$c/max_state" 2>/dev/null)" "$w"
done
lsmod | grep -E 'intel_tcc|int340x|proc_thermal|intel_rapl|coretemp|x86_pkg_temp|msr' || true

sec "hwmon (датчики)"
for h in /sys/class/hwmon/hwmon*; do
  echo "--- $(basename "$h"): $(cat "$h/name")"
  for i in "$h"/*_input; do
    [ -e "$i" ] || continue
    l="${i%_input}_label"
    printf '  %s %s = %s\n' "$(basename "$i")" "$( [ -e "$l" ] && cat "$l")" "$(cat "$i" 2>/dev/null)"
  done
  show "$h"/pwm* "$h"/fan*_target
done

sec "Встроенная графика"
for d in /sys/class/drm/card[0-9]; do
  echo "--- $d"
  readlink -f "$d/device/driver"
  cat "$d/device/vendor" "$d/device/device" 2>/dev/null | tr '\n' ' '; echo
  show "$d"/gt_{cur,act,min,max,boost,RP0,RP1,RPn}_freq_mhz
  show "$d"/device/tile0/gt0/freq0/{cur,act,min,max,rp0,rpe,rpn}_freq 2>/dev/null
  for h in "$d"/device/hwmon/hwmon*; do [ -d "$h" ] && show "$h"/*; done
done
command -v intel_gpu_top >/dev/null && echo "intel_gpu_top: есть" || echo "intel_gpu_top: нет (пакет intel-gpu-tools)"

sec "Управление питанием: демоны"
for s in power-profiles-daemon tlp auto-cpufreq tuned thermald throttled intel-undervolt; do
  printf '%-22s %s / %s\n' "$s" "$(systemctl is-active $s 2>/dev/null)" "$(systemctl is-enabled $s 2>/dev/null)"
done
command -v powerprofilesctl >/dev/null && { powerprofilesctl version 2>/dev/null; powerprofilesctl list; }
cat /var/lib/power-profiles-daemon/state.ini 2>/dev/null

sec "Питание: батарея / сеть"
for p in /sys/class/power_supply/*; do
  echo "--- $p"
  show "$p"/{type,online,status,capacity,energy_now,energy_full,energy_full_design,charge_now,charge_full,charge_full_design,power_now,current_now,current_avg,voltage_now,voltage_min_design,cycle_count,charge_control_end_threshold,charge_control_start_threshold}
done

sec "Платформенные профили (ACPI)"
show /sys/firmware/acpi/platform_profile /sys/firmware/acpi/platform_profile_choices
ls /sys/class/platform-profile/ 2>/dev/null

sec "Нужные утилиты"
for b in turbostat powertop rdmsr wrmsr intel_gpu_top sensors btop python3; do
  printf '%-14s %s\n' "$b" "$(command -v $b || echo НЕТ)"
done
python3 -c "import sys; print('python', sys.version.split()[0])"
for m in gi textual rich psutil; do python3 -c "import $m" 2>/dev/null && echo "py:$m есть" || echo "py:$m нет"; done

sec "Готово"
echo "Файл: $OUT"
[ -n "${SUDO_USER:-}" ] && chown "$SUDO_USER": "$OUT"
