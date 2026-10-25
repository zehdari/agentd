#!/bin/bash
# Patched copy of KasmVNC's web client that Caddy serves for linux.zehdari.com (the websocket still goes to
# Xkasmvnc): hide the control-panel arrow on mouse/keyboard devices; kasm-keys.js adds Ctrl+Shift+1 (panel)
# and Ctrl+Shift+Enter (fullscreen). Touch devices keep the arrow. Re-run after a KasmVNC upgrade.
set -eu
OUT=${1:-/var/lib/agentd-kasm-www}
sudo rsync -a --delete /usr/share/kasmvnc/www/ "$OUT/"
sudo cp "$(dirname "$0")/kasm-keys.js" "$OUT/kasm-keys.js"
sudo sed -i 's#</head>#<style>@media (any-pointer: fine) { html:not(.kasm-show-bar) \#noVNC_control_bar_anchor { display: none !important } }</style><script src="kasm-keys.js"></script></head>#' "$OUT/index.html"
grep -q 'any-pointer: fine' "$OUT/index.html" && echo "patched $OUT"
