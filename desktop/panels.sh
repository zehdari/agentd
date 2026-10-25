#!/bin/bash
# Rebuild the remote desktop's XFCE panels from scratch (Ubuntu-like): one dark top bar (menu, Firefox/Files/Terminal,
# window list, clock, tray). No side dock: a left strut made xfdesktop's icon grid shift on every resize.
# Run inside the session: DISPLAY=:5 with its DBus address (see `ds` below) — or via panels.sh from session.sh.
set -u
Q="xfconf-query -c xfce4-panel"
pkill -x xfce4-panel; sleep 0.5
$Q -p /plugins -rR 2>/dev/null; $Q -p /panels -rR 2>/dev/null; rm -rf ~/.config/xfce4/panel/launcher-*
n=0
plugin() { n=$((n+1)); $Q -p /plugins/plugin-$n -n -t string -s "$1"; shift
  while [ $# -gt 0 ]; do $Q -p /plugins/plugin-$n/$1 -n -t $2 -s "$3"; shift 3; done; ids+=($n); }
launcher() { plugin launcher; mkdir -p ~/.config/xfce4/panel/launcher-$n
  cp /usr/share/applications/$1.desktop ~/.config/xfce4/panel/launcher-$n/; $Q -p /plugins/plugin-$n/items -n -a -t string -s $1.desktop; }
panel() {   # panel <id> <position> <mode> <size> <icon-size> <alpha>
  local P=/panels/panel-$1
  $Q -p $P/position -n -t string -s "$2"; $Q -p $P/mode -n -t uint -s $3
  $Q -p $P/size -n -t uint -s $4; $Q -p $P/icon-size -n -t uint -s $5
  $Q -p $P/length -n -t uint -s 100; $Q -p $P/position-locked -n -t bool -s true
  $Q -p $P/background-style -n -t uint -s 1
  $Q -p $P/background-rgba -n -t double -t double -t double -t double -s 0.11 -s 0.11 -s 0.11 -s $6
  local a=(); for x in "${ids[@]}"; do a+=(-t int -s $x); done; $Q -p $P/plugin-ids -n -a "${a[@]}"
}
ids=()
plugin whiskermenu button-title string "Activities" show-button-title bool true show-button-icon bool false
launcher firefox; launcher org.gnome.Nautilus; launcher org.gnome.Terminal
plugin separator style uint 1
plugin tasklist show-labels bool true flat-buttons bool true
plugin separator expand bool true style uint 0
plugin clock digital-format string "%a %b %-d  %-I:%M %p" mode uint 2
plugin separator expand bool true style uint 0
plugin systray
plugin pulseaudio
plugin actions appearance uint 0
panel 1 "p=6;x=0;y=0" 0 34 22 1
$Q -p /panels -n -a -t int -s 1
setsid xfce4-panel >/dev/null 2>&1 &
