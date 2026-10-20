---
version: 1
slug: "mockups-dashboard-index-html"
primary_target: "mockups/dashboard/index.html"
related_targets: []
---

# Surface: agentd web dashboard (mockup)

Mode: Operate. Static mockup for screenshots; Discord stays the live control surface.

Audience/job: the machine's single owner, mostly on a phone, checks what agents are doing, answers the ones waiting, stops runaways, and checks machine health and long jobs.
Constraints: only capabilities agentd has today (new, steer, stop, effort, info, sh, files, alerts, activity); sample data is from state.json titles plus labeled sample values.
Unresolved: whether this becomes a real build behind agentd/server.py over Tailscale.

## Direction contract

THESIS: Cam-PC read as a nautical chart: agents are vessels on tracks, and machine load is shown as depth soundings. It turns down the default dark terminal-SaaS dashboard (near-black ground, neon accent, card grid).
OWN-WORLD: NOAA chart paper: white "deep water" ground, buff land tint on the shell rail, shoal blues for load bands, chart magenta used only for "needs you", black hairlines, and italic numerals for soundings. Public Sans (the US federal face) roman for labels, italic for measurements.
STORY: In one glance the owner sees who is waiting on them, what is running and how far along it is, and whether the machine has headroom. Then they answer, steer, or stop.
FIRST VIEWPORT: Buff left rail (Cam-PC, nav, service lights). Top bar with search and New agent. A magenta "Waiting on you" band at the top of the main column. Below it, the agents table, where each row carries its track line on one shared 2-hour scale. Right column: machine soundings (GPU/CPU/RAM as bathymetric bands), long jobs, and files sent. Mobile collapses to waiting → running → machine.
FORM: Nautical chart (NOAA), my grounded list #3 of 7. seed:5225fdea0f4bef40
Raises: Japanese high-density web → hairline-ruled density, 13px base, no padded cards. Orienteering map → one reserved accent (magenta = needs you, nothing else). Terminal wayfinding → mobile leads with the single next decision. Botanical folio → every track on the same fixed time scale. Character catalog → finished agents print desaturated. Type specimen → hierarchy by scale and weight alone.
FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
