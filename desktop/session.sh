#!/bin/bash
# The remote desktop behind the dashboard's "Remote desktop" button: a TigerVNC X server (no physical
# display, so it works while the GPU is passed to the Windows VM) running XFCE. VNC is only on a 0600
# unix socket; agentd's web dashboard bridges it to the browser after its usual Tailscale check.
set -u
DISP=${AGENTD_DESKTOP_DISPLAY:-:5}
SOCK=${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/agentd-desktop.sock
GEOM=${AGENTD_DESKTOP_GEOMETRY:-1920x1080}
rm -f "$SOCK" "/tmp/.X11-unix/X${DISP#:}" "/tmp/.X${DISP#:}-lock" 2>/dev/null
unset WAYLAND_DISPLAY XDG_SESSION_ID XDG_SESSION_TYPE XDG_SESSION_DESKTOP XDG_CURRENT_DESKTOP DESKTOP_SESSION GNOME_SETUP_DISPLAY SESSION_MANAGER DBUS_SESSION_BUS_ADDRESS

Xvnc "$DISP" -geometry "$GEOM" -depth 24 -rfbport -1 -rfbunixpath "$SOCK" -rfbunixmode 0600 \
  -SecurityTypes None -AlwaysShared -AcceptSetDesktopSize -nolisten tcp -dpi 96 &
XPID=$!
trap 'kill $XPID 2>/dev/null' EXIT
for _ in $(seq 50); do [ -S "$SOCK" ] && break; sleep 0.1; done

export DISPLAY=$DISP XDG_CURRENT_DESKTOP=XFCE XDG_SESSION_TYPE=x11
vncconfig -nowin &          # clipboard between noVNC and X
dbus-run-session -- startxfce4 &
wait -n $XPID $!            # either one dying ends the unit; systemd restarts it
