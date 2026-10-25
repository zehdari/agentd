// Added to KasmVNC's web client by kasm-www.sh. Capture-phase so the remote desktop never sees these keys:
//   Ctrl+Shift+1      show/hide the control panel (its arrow is hidden on mouse/keyboard devices)
//   Ctrl+Shift+Enter  toggle browser fullscreen
// KasmVNC closes its control bar on keyboard activity, so the panel toggles a moment after the keypress.
const ours = (e) => e.ctrlKey && e.shiftKey && !e.altKey && !e.metaKey && (e.code === "Digit1" || e.code === "Enter");
for (const type of ["keydown", "keyup", "keypress"]) {
  window.addEventListener(type, (e) => {
    if (!ours(e)) return;
    e.preventDefault(); e.stopImmediatePropagation();
    if (type !== "keydown" || e.repeat) return;
    if (e.code === "Enter") {
      if (document.fullscreenElement) document.exitFullscreen(); else document.documentElement.requestFullscreen();
      return;
    }
    const open = document.documentElement.classList.toggle("kasm-show-bar");
    setTimeout(() => document.getElementById("noVNC_control_bar")?.classList.toggle("noVNC_open", open), 250);
  }, true);
}
