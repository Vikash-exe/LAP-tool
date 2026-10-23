#!/usr/bin/env bash
# Is THIS project's dashboard backend running? Other uvicorn processes on the machine (another app, CVAT, a copy of
# this project in another folder) are told apart by their working folder, which Linux records in /proc/<pid>/cwd.
# This project's backend always runs inside its own web/ folder (that is where run_backend.sh starts it).
# Usage: web/status.sh        (no root needed; stopping it: web/status.sh stop)
HERE="$(cd "$(dirname "$0")" && pwd)"
mine=()

echo "This project: $HERE"
echo
for pid in $(pgrep -f "uvicorn"); do
  cwd="$(readlink "/proc/$pid/cwd" 2>/dev/null)"
  if [ "$cwd" = "$HERE" ]; then mine+=("$pid"); fi
done

if [ "${#mine[@]}" -eq 0 ]; then
  echo "NOT RUNNING: no backend of this project is running."
else
  for pid in "${mine[@]}"; do
    port="$(ss -ltnp 2>/dev/null | grep "pid=$pid," | awk '{print $4}' | head -1)"
    echo "RUNNING   pid $pid   user $(ps -o user= -p "$pid")   started $(ps -o lstart= -p "$pid")   listening on ${port:-?}"
    if [ -n "$port" ]; then
      echo -n "          health: "
      curl -s -m 5 "http://127.0.0.1:${port##*:}/api/health" || echo "no answer"
      echo
    fi
  done
fi

echo
echo "Other uvicorn processes on this machine (NOT this project):"
others=0
for pid in $(pgrep -f "uvicorn"); do
  case " ${mine[*]} " in *" $pid "*) continue ;; esac
  others=1
  echo "  pid $pid   user $(ps -o user= -p "$pid")   folder $(readlink "/proc/$pid/cwd" 2>/dev/null || echo '(not visible)')"
  echo "            $(ps -o args= -p "$pid" | cut -c1-100)"
done
[ "$others" -eq 1 ] || echo "  none"

# web/status.sh stop : stops only this project's backend(s), never the others.
if [ "${1:-}" = "stop" ] && [ "${#mine[@]}" -gt 0 ]; then
  echo
  kill "${mine[@]}" && echo "Stopped: ${mine[*]}"
fi
