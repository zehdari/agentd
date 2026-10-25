"use strict";
/* Remote desktop links, shared by the dashboard's Desktop page and /rd/open/{linux,windows}
   (where linux.zehdari.com and windows.zehdari.com land). noVNC's WebSocket is bridged by agentd,
   behind the same Tailscale check as the dashboard. A touch device gets the screen scaled to fit (the noVNC
   handle on the left has the keyboard); a laptop gets the Linux desktop resized to match the browser window. */
const touchDevice = () => matchMedia("(pointer: coarse)").matches && !matchMedia("(any-pointer: fine)").matches;
function desktopUrl(which) {
  if (which === "linux") return "https://linux.zehdari.com/";   // KasmVNC's own web client (Caddy, tailnet only)
  const fit = which === "linux" && !touchDevice() ? "remote" : "scale";   // the VM's screen can't be resized from here
  return `/rd/novnc/vnc.html?autoconnect=1&reconnect=1&reconnect_delay=2000&show_dot=1&resize=${fit}&path=rd/ws/${which}`;
}
