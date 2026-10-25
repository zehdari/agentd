#!/bin/bash
# Patched copy of KasmVNC's web client that Caddy serves for linux.zehdari.com (the websocket still goes to
# Xkasmvnc): hide the control-panel arrow on mouse/keyboard devices; kasm-keys.js adds Ctrl+Shift+1 (panel)
# and Ctrl+Shift+Enter (fullscreen); Mac Cmd is sent as Super, not translated to Ctrl. Touch devices keep the arrow. Re-run after a KasmVNC upgrade.
set -eu
OUT=${1:-/var/lib/agentd-kasm-www}
sudo rsync -a --delete /usr/share/kasmvnc/www/ "$OUT/"
sudo cp "$(dirname "$0")/kasm-keys.js" "$OUT/kasm-keys.js"
sudo sed -i 's#</head>#<style>@media (any-pointer: fine) { html:not(.kasm-show-bar) \#noVNC_control_bar_anchor { display: none !important } }</style><script src="kasm-keys.js"></script></head>#' "$OUT/index.html"
# Cmd stays Cmd (Super): KasmVNC's default turns Mac Cmd+key into Ctrl+key
sudo sed -i 's/initSetting("translate_shortcuts",!0)/initSetting("translate_shortcuts",!1)/' "$OUT"/assets/*.js
# ...and its Mac keymap sends left Cmd as Alt and Option as Mode_switch; send Cmd as Super, Option as Alt
sudo sed -i 's/case O.XK_Super_L:r=O.XK_Alt_L;break;/case O.XK_Super_L:r=O.XK_Super_L;break;/; s/case O.XK_Super_R:r=O.XK_Super_L;break;/case O.XK_Super_R:r=O.XK_Super_R;break;/; s/case O.XK_Alt_L:r=O.XK_Mode_switch;break;/case O.XK_Alt_L:r=O.XK_Alt_L;break;/; s/case O.XK_Alt_R:r=O.XK_ISO_Level3_Shift;break}/case O.XK_Alt_R:r=O.XK_Alt_R;break}/' "$OUT"/assets/*.js
# cache-bust the entry scripts (Chrome's reload doesn't revalidate subresources it already has)
V=$(cat "$OUT"/assets/*.js "$OUT"/kasm-keys.js | md5sum | cut -c1-8)
sudo sed -i -E "s#(src=\"[^\"]+\.js)\"#\1?v=$V\"#g" "$OUT/index.html"
grep -lq 'case O.XK_Super_L:r=O.XK_Super_L;break;' "$OUT"/assets/*.js &&
grep -q 'any-pointer: fine' "$OUT/index.html" && grep -lq 'initSetting("translate_shortcuts",!1)' "$OUT"/assets/*.js && echo "patched $OUT"
