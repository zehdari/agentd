// The theme and sidebar buttons on artifact pages, working like the dashboard's. Sandboxed pages can't store
// anything: the dashboard's viewer passes its settings in the URL and takes changes back, and a page on its
// own tab keeps them in window.name.
(() => {
  const root = document.documentElement, query = new URLSearchParams(location.search), embedded = parent !== window;
  let saved = {};
  try { saved = JSON.parse(window.name) || {}; } catch { /* a name we didn't set */ }
  let theme = saved.theme || query.get("theme") || "auto", rail = saved.rail || query.get("rail") || "shown";
  const paths = {
    sidebar: '<rect x="3" y="4" width="18" height="16" rx="1"/><path d="M9 4v16"/>',
    moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    auto: '<circle cx="12" cy="12" r="9"/><path d="M12 3v18a9 9 0 0 0 0-18z" fill="currentColor"/>',
  };
  const icon = name => `<svg width="16" height="16" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${paths[name]}</svg>`;
  const apply = () => {
    root.style.colorScheme = theme === "auto" ? "" : theme;
    root.dataset.rail = rail;
  };
  const remember = () => {
    window.name = JSON.stringify({ theme, rail });
    if (embedded) parent.postMessage({ agentd: { theme, rail } }, "*");
  };
  apply();

  addEventListener("DOMContentLoaded", () => {
    const page = document.querySelector(".page");
    if (!page) return;
    const toc = page.querySelector(".toc") || page.insertBefore(document.createElement("nav"), page.firstChild);
    toc.classList.add("toc");
    const themeBtn = toc.appendChild(document.createElement("button"));
    themeBtn.type = "button";
    themeBtn.className = "theme-toggle";
    const railBtn = document.body.appendChild(document.createElement("button"));
    railBtn.type = "button";
    railBtn.className = "rail-toggle";
    railBtn.innerHTML = icon("sidebar");
    const draw = () => {
      themeBtn.innerHTML = `${icon(theme === "auto" ? "auto" : theme === "dark" ? "moon" : "sun")}<span>Theme: ${theme}</span>`;
      const label = rail === "hidden" ? "Show sidebar" : "Hide sidebar";
      railBtn.title = label;
      railBtn.setAttribute("aria-label", label);
      railBtn.setAttribute("aria-expanded", rail !== "hidden");
    };
    draw();
    themeBtn.addEventListener("click", () => {
      theme = { auto: "light", light: "dark", dark: "auto" }[theme] || "auto";
      apply(); draw(); remember();
    });
    railBtn.addEventListener("click", () => {
      rail = rail === "hidden" ? "shown" : "hidden";
      apply(); draw(); remember();
    });
  });
})();
