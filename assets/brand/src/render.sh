#!/bin/zsh
# render.sh page.html out.png W H [scale]
cd "$(dirname $0)"
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --disable-gpu --hide-scrollbars \
  --force-device-scale-factor=${5:-1} --window-size=$3,$4 --virtual-time-budget=4000 \
  --screenshot="$2" "http://localhost:${PORT:-8765}/$1" 2>/dev/null
