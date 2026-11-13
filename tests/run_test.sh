#!/usr/bin/env bash
# run_test.sh — ручные тесты записи параметров питания.
# Каждый тест: замер под нагрузкой ДО -> применение -> чтение назад -> замер ПОСЛЕ ->
# пауза и повторное чтение (не перебила ли прошивка/ppd) -> ВОЗВРАТ исходного значения.
#
# Запуск: sudo ./tests/run_test.sh <тест> [значение]
#   rapl  [Вт]     PL1+PL2 пакета через intel-rapl (MSR)           по умолч. 5
#   mmio  [Вт]     то же через intel-rapl-mmio (прошивочная зона)  по умолч. 5
#   tcc   [°C]     TCC offset: троттлинг с (Tjmax - offset)       по умолч. 50
#   turbo          no_turbo=1
#   perf  [%]      max_perf_pct                                   по умолч. 50
#   epp   [знач.]  EPP для всех потоков                           по умолч. power
#   gpu   [МГц]    потолок частоты iGPU (gt_max_freq_mhz)         по умолч. 500
#   all            все тесты по очереди со значениями по умолчанию
#
# Лучше запускать от сети: тесты гоняют все ядра на 100%.

set -u
[ "$EUID" -eq 0 ] || { echo "Запустите через sudo: sudo $0 $*"; exit 1; }

HERE="$(cd "$(dirname "$0")" && pwd)"
OUTDIR="$HERE/../results"; mkdir -p "$OUTDIR"
HOST="$(cat /etc/hostname 2>/dev/null || uname -n)"

LOAD_S=${LOAD_S:-15}     # длительность замера под нагрузкой
HOLD_S=${HOLD_S:-30}     # пауза перед проверкой, не откатилось ли значение

RAPL=/sys/class/powercap/intel-rapl:0
MMIO=/sys/class/powercap/intel-rapl-mmio:0
PST=/sys/devices/system/cpu/intel_pstate
TCC_DEV=$(grep -l "^TCC Offset$" /sys/class/thermal/cooling_device*/type 2>/dev/null | head -1 | xargs -r dirname)
GPU=$(ls -d /sys/class/drm/card[0-9] 2>/dev/null | while read d; do [ -e "$d/gt_max_freq_mhz" ] && echo "$d" && break; done)
CORETEMP=$(grep -l "^coretemp$" /sys/class/hwmon/hwmon*/name 2>/dev/null | head -1 | xargs -r dirname)

# ---------- служебное ----------
LOAD_PIDS=()
RESTORE=()   # строки вида "путь|значение", восстанавливаются в обратном порядке

load_start() { for _ in $(seq "$(nproc)"); do ( while :; do :; done ) & LOAD_PIDS+=($!); done; }
load_stop()  { [ ${#LOAD_PIDS[@]} -gt 0 ] && kill "${LOAD_PIDS[@]}" 2>/dev/null; wait 2>/dev/null; LOAD_PIDS=(); }

remember() { for f in "$@"; do RESTORE+=("$f|$(cat "$f")"); done; }
restore_all() {
  local i e f v
  for (( i=${#RESTORE[@]}-1; i>=0; i-- )); do
    e=${RESTORE[$i]}; f=${e%%|*}; v=${e#*|}
    echo "$v" > "$f" 2>/dev/null && echo "  вернул $f = $v" || echo "  !! НЕ УДАЛОСЬ вернуть $f = $v"
  done
  RESTORE=()
}
cleanup() { load_stop; if [ ${#RESTORE[@]} -gt 0 ]; then echo "Восстановление:"; restore_all; fi; }
trap cleanup EXIT
trap 'exit 130' INT TERM

wr() { # wr <файл> <значение>: запись с проверкой
  if echo "$2" > "$1" 2>/tmp/wr_err; then
    local got; got=$(cat "$1")
    echo "  запись $1 = $2 -> прочитано: $got $( [ "$got" = "$2" ] && echo OK || echo 'НЕ СОВПАДАЕТ')"
  else
    echo "  !! ОШИБКА записи $1 = $2: $(cat /tmp/wr_err)"; return 1
  fi
}

msr_tcc() { modprobe msr 2>/dev/null; python3 -c '
import os,struct
fd=os.open("/dev/cpu/0/msr",os.O_RDONLY); v=struct.unpack("<Q",os.pread(fd,8,0x1A2))[0]
print(f"MSR 0x1A2: Tjmax={(v>>16)&0xff} offset={(v>>24)&0x3f} lock={(v>>31)&1}")'; }
msr_pl() { modprobe msr 2>/dev/null; python3 -c '
import os,struct
fd=os.open("/dev/cpu/0/msr",os.O_RDONLY)
u=1/(1<<(struct.unpack("<Q",os.pread(fd,8,0x606))[0]&0xf)); v=struct.unpack("<Q",os.pread(fd,8,0x610))[0]
print(f"MSR 0x610: PL1={(v&0x7fff)*u:.2f}W PL2={((v>>32)&0x7fff)*u:.2f}W lock={(v>>63)&1}")'; }

# measure <сек>: средняя мощность пакета, средняя частота CPU, макс. t пакета, средняя частота GPU
measure() {
  local s=$1 e1 e2 i f sum n tmax=0 t gsum=0 gn=0
  e1=$(cat $RAPL/energy_uj); sum=0; n=0
  for (( i=0; i<s; i++ )); do
    sleep 1
    for f in /sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq; do sum=$((sum+$(cat $f))); n=$((n+1)); done
    t=$(cat "$CORETEMP/temp1_input"); [ "$t" -gt "$tmax" ] && tmax=$t
    [ -n "$GPU" ] && { gsum=$((gsum+$(cat $GPU/gt_act_freq_mhz))); gn=$((gn+1)); }
  done
  e2=$(cat $RAPL/energy_uj)
  printf '    мощность пакета: %d.%02d Вт | ср. частота CPU: %d МГц | макс. t: %d °C' \
    $(( (e2-e1)/s/1000000 )) $(( (e2-e1)/s/10000 % 100 )) $(( sum/n/1000 )) $(( tmax/1000 ))
  [ "$gn" -gt 0 ] && printf ' | iGPU: %d МГц' $((gsum/gn))
  echo
}
under_load() { echo "  [$1] нагрузка ${LOAD_S} с на $(nproc) потоков..."; load_start; measure "$LOAD_S"; load_stop; sleep 3; }

hold_check() { # hold_check <файл> <ожидаемое>
  echo "  жду ${HOLD_S} с без нагрузки, не откатит ли кто-то значение..."
  sleep "$HOLD_S"
  local got; got=$(cat "$1")
  echo "  через ${HOLD_S} с: $1 = $got $( [ "$got" = "$2" ] && echo '(держится)' || echo '!! ИЗМЕНИЛОСЬ')"
}

# ---------- тесты ----------
t_rapl() { # $1 зона, $2 Вт
  local z=$1 w=${2:-5} uw
  [ -d "$z" ] || { echo "  нет зоны $z"; return; }
  uw=$((w*1000000))
  echo "  было: PL1=$(cat $z/constraint_0_power_limit_uw) PL2=$(cat $z/constraint_1_power_limit_uw)"; msr_pl
  under_load "ДО"
  remember $z/constraint_0_power_limit_uw $z/constraint_1_power_limit_uw
  wr $z/constraint_1_power_limit_uw $uw; wr $z/constraint_0_power_limit_uw $uw; msr_pl
  under_load "ПОСЛЕ ${w} Вт"
  hold_check $z/constraint_0_power_limit_uw $uw
  restore_all; msr_pl
}

t_tcc() {
  local off=${1:-50}
  [ -n "$TCC_DEV" ] || { echo "  нет устройства TCC Offset"; return; }
  local max; max=$(cat $TCC_DEV/max_state); [ "$off" -gt "$max" ] && { echo "  offset $off > max $max, беру $max"; off=$max; }
  msr_tcc; under_load "ДО"
  remember $TCC_DEV/cur_state
  wr $TCC_DEV/cur_state "$off"; msr_tcc
  under_load "ПОСЛЕ offset $off (троттлинг с $((100-off)) °C)"
  hold_check $TCC_DEV/cur_state "$off"
  restore_all; msr_tcc
}

t_simple() { # $1 файл(ы) через пробел, $2 значение, $3 подпись
  local files=$1 v=$2 f first
  first=${files%% *}
  echo "  было: $first = $(cat $first)"
  under_load "ДО"
  remember $files
  for f in $files; do wr "$f" "$v" >/dev/null || echo "  !! ошибка записи $f"; done
  echo "  записано '$v' в $(echo $files | wc -w) файл(ов), сейчас: $first = $(cat $first)"
  under_load "ПОСЛЕ $3"
  hold_check "$first" "$v"
  restore_all >/dev/null; echo "  возвращено: $first = $(cat $first)"
}

t_gpu() {
  local mhz=${1:-500}
  [ -n "$GPU" ] || { echo "  нет iGPU с gt_max_freq_mhz"; return; }
  echo "  было: max=$(cat $GPU/gt_max_freq_mhz) boost=$(cat $GPU/gt_boost_freq_mhz) RPn..RP0=$(cat $GPU/gt_RPn_freq_mhz)..$(cat $GPU/gt_RP0_freq_mhz)"
  remember $GPU/gt_boost_freq_mhz $GPU/gt_max_freq_mhz
  [ "$mhz" -lt "$(cat $GPU/gt_boost_freq_mhz)" ] && wr $GPU/gt_boost_freq_mhz "$mhz"
  wr $GPU/gt_max_freq_mhz "$mhz"
  echo "  (нагрузку на GPU скрипт не создаёт: если хотите проверить, запустите видео/glxgears и смотрите iGPU в замере)"
  measure 5
  hold_check $GPU/gt_max_freq_mhz "$mhz"
  restore_all
}

run() {
  local name=$1; shift
  echo; echo "########## ТЕСТ: $name $* ##########"
  echo "профиль ppd: $(powerprofilesctl get 2>/dev/null) | питание от сети: $(cat /sys/class/power_supply/A*/online 2>/dev/null | head -1)"
  case $name in
    rapl)  t_rapl $RAPL "${1:-5}" ;;
    mmio)  t_rapl $MMIO "${1:-5}" ;;
    tcc)   t_tcc "${1:-50}" ;;
    turbo) t_simple "$PST/no_turbo" 1 "no_turbo=1" ;;
    perf)  t_simple "$PST/max_perf_pct" "${1:-50}" "max_perf_pct=${1:-50}" ;;
    epp)   t_simple "$(echo /sys/devices/system/cpu/cpu[0-9]*/cpufreq/energy_performance_preference)" "${1:-power}" "EPP=${1:-power}" ;;
    gpu)   t_gpu "${1:-500}" ;;
    *) echo "неизвестный тест: $name"; return 1 ;;
  esac
}

[ $# -ge 1 ] || { sed -n '2,17p' "$0"; exit 1; }
OUT="$OUTDIR/test-$1-$HOST-$(date +%Y%m%d-%H%M%S).txt"
# вывод в файл через tee, но без пайплайна: иначе trap не сработает по Ctrl+C и значения не вернутся
exec > >(tee "$OUT") 2>&1
echo "$(date) | $(grep -m1 'model name' /proc/cpuinfo | cut -d: -f2) | ядро $(uname -r)"
if [ "$1" = all ]; then
  for t in rapl mmio tcc turbo perf epp gpu; do run $t; done
else
  run "$@"
fi
echo; echo "Готово. Лог: $OUT"
[ -n "${SUDO_USER:-}" ] && chown "$SUDO_USER": "$OUT"
