---
name: agentd-artifact
description: Publish an HTML page at a private agentd.zehdari.com link with one command; hosting is already handled, so never start a web server. Use when the user asks for an artifact, page, report, dashboard, interactive demo, or a link to something, or when a result (comparison, run summary, charts, explainer, tool) would read better as a page than as chat text or attached files. Not for a single image, log or data file; send those with agentd-send.
---

# Artifact pages

Follow these steps as written. There is nothing to set up or look into.

```bash
mkdir -p ~/artifacts/reward-report                    # short name for this page
cp ~/.claude/skills/agentd-artifact/template.html ~/artifacts/reward-report/index.html
# fill in the body, deleting what you don't use; put images, data or JS beside it
agentd-artifact publish ~/artifacts/reward-report     # prints the link
agentd-artifact shot LINK                             # phone view; prints a PNG path, Read it
agentd-artifact shot LINK --width 1440 --height 900   # computer view; Read it and fix what's wrong
```

Then put the link in your reply on its own line, with one sentence on what's on the page. Don't repeat the page in chat.

**To change a page:** edit the same folder and run the same `publish`. The link stays the same. Ask `agentd-artifact list` if you need to find a link.

**To delete a page:** `agentd-artifact rm LINK`.

Hosting ends there. Don't start a server, edit Caddy, add a systemd unit, open a port or use GitHub Pages. The page is on the dashboard's Artifacts menu automatically. Links open on the user's tailnet devices only.

## Writing the page

The template loads agentd's theme, so pages match the dashboard in light and dark and fill the width on a computer. Keep its two `/a/theme/` lines and don't write your own colours, fonts or layout. Everything below is already styled:

| Use | Markup |
|---|---|
| Sidebar on a computer, hidden on a phone | `<nav class="toc">` with one link per section |
| The answer up top | `<header>` with `h1` and `<p class="lead">` |
| Key numbers | `.cards` of `.card`, each `<b>` then `<span>`; add `.good`, `.bad` or `.key` to a card |
| The one thing to act on | `<div class="callout">` |
| Two things side by side | `<section class="split">` with two children |
| Charts and images | `<figure>` with `<figcaption>` |
| Tables | `<div class="table"><table>` |
| Small print, numbers | `.note`, `.mono` |

Add page-specific CSS only for something the theme doesn't cover, using its variables: `--ink`, `--ink-2`, `--ink-3`, `--rule`, `--surface`, `--deep`, and for chart series `--channel`, `--magenta`, `--green`, `--red`, `--contour`. Use `var(--...)` in SVG too, so charts follow the theme. Read colours in JS with `getComputedStyle(document.documentElement).getPropertyValue("--channel")`.

The page runs in a CSP sandbox, because it shares a domain with the dashboard.

- **Works:** scripts, inline or as files; ES modules; `fetch("./data.json")`; images and video; forms; popups; downloads; CDN scripts.
- **Doesn't work:** `localStorage`, `sessionStorage`, cookies and IndexedDB, and touching `localStorage` throws. Keep state in memory or the URL hash.
- **Paths:** your own files are always relative, `./plot.png`, never `/plot.png`. The only outside paths are the two theme lines; `/static` and `/api` are off limits.
- **Canvas or WebGL:** set `crossOrigin = "anonymous"` on images you draw from.
- **Skipped when publishing:** dotfiles, `node_modules`, `__pycache__`.

The user reads these on an iPhone and on a computer. Lead with the answer, then the evidence. For charts, load the `dataviz` skill first.

## Privacy

Anyone with the link can read the page. Keep secrets, tokens and personal data out of it.
