# Job Hunting Project

Noah's OE (overemployment) strategy — stacking remote jobs for early retirement.

## Current Status
Active job searching with automated application assistance. Infrastructure in place for autonomous applications.

## Applications Submitted (7 total)
| Company | Role | Salary | ATS | Date |
|---------|------|--------|-----|------|
| XBOW | Research Engineer / Platform Infrastructure | - | - | 2026-01-30 |
| Chainalysis | Security Engineer, Product Infrastructure | $108k-$205k | - | 2026-01-31 (uncertain) |
| AcuityMD | Software Engineer, Infrastructure | $180k-$210k | Greenhouse | 2026-01-31 |
| Sequen AI | Staff SWE Infrastructure | $250k-$300k | Ashby | 2026-01-31 |
| Found | Senior SWE Data Infrastructure | $181k-$241k | - | 2026-01-31 |
| Persona | Software Engineer, Compute | $130k-$220k | - | 2026-01-31 |

## Retry Queue
- Oscar Health — Greenhouse resume upload issue
- Leidos — Workday timeout
- ICMARC — Workday partial completion

## Automation Infrastructure
- **MR #25:** Chromium in moltbot container for autonomous applications
- **Retry system:** `retry-queue.json` → 3 AM cron → creative retry strategies
- **Nightly search:** 2 AM cron when laptop node available
- **xvfb consideration:** For xdotool compatibility + stealth

## Criteria
- Remote US positions only
- $200k+ salary range
- Infrastructure/Platform/DevOps roles

## Known ATS Challenges
- **Ashby:** React state issues — autofill helps update state
- **Workday:** Unresponsive buttons — hover-then-click workaround
- **Greenhouse:** Resume upload sometimes fails — xdotool native interaction

## Cost Estimate
- Simple form: ~$1-2/application
- Complex (Greenhouse/Workday): ~$3-4/application

---
*Last synthesized: 2026-02-08*

## Facts
- XBOW application submitted - Research Engineer / Platform Infrastructure role (milestone, 2026-01-30)
- Chainalysis application submitted - Security Engineer, Product Infrastructure ($108k-$205k) - status uncertain (milestone, 2026-01-31)
- ICMARC/Workday application in progress - discovered hover-then-click workaround for unresponsive buttons (status, 2026-01-31)
- Created MR #25 to add Chromium to moltbot container for autonomous job applications (milestone, 2026-01-31)
- Considering xvfb addition to container for xdotool compatibility and reduced captcha detection risk (status, 2026-01-31)
- AcuityMD application submitted - Software Engineer, Infrastructure ($180k-$210k) via Greenhouse (milestone, 2026-01-31)
- Sequen AI application submitted - Staff SWE Infrastructure ($250k-$300k) via Ashby (milestone, 2026-01-31)
- Found application submitted - Senior SWE Data Infrastructure ($181k-$241k) - Fintech for self-employed (milestone, 2026-01-31)
- Persona application submitted - Software Engineer, Compute ($130k-$220k) - Identity verification (milestone, 2026-01-31)
- Set up automated retry system: retry-queue.json, abandoned.json, 3 AM cron (milestone, 2026-01-31)
- Oscar Health, Leidos, Icmarc in retry queue (status, 2026-01-31)
- Set up job-search-nightly cron (2 AM) for proactive searching when laptop node is up (milestone, 2026-02-01)
- Job hunting skill refactored: 1635→641 lines. All agent-browser, removed node/xdotool paths. Scout uses OpenClaw browser tool, workers use agent-browser CLI. (milestone, 2026-02-12)
- Resume located at /uploads/Resume (Kontur, Noah).pdf in chromium container. Volume mounted via MR !249. (status, 2026-02-12)
- Job hunting skill refactored from 1622 to 642 lines - all agent-browser, no node/xdotool (milestone, 2026-02-13)
- Resume uploaded to chromium container at /uploads/Resume (Kontur, Noah).pdf (status, 2026-02-13)
- CrowdStrike rejected - listed as Hybrid despite showing Remote on Hiring Cafe (status, 2026-02-13)
- Gmail OAuth working for workers to fetch verification codes autonomously (milestone, 2026-02-13)
- Total applications: 55 as of Feb 18, 2026 (milestone, 2026-02-18)
- 2captcha + Capsolver CAPTCHA solving integration built (solve-captcha.sh). Capsolver is primary solver. (milestone, 2026-02-18)
- Feb 18 batch: CrowdStrike, GM, Calix, Rad AI, Virta Health, HackerOne, Medallion submitted. Luma AI blocked by hCaptcha. (milestone, 2026-02-18)
- 13 jobs remaining in queue: Camunda, Kalepa, Temporal, Oscar Health, Reddit, Gauntlet, Veeam, Abnormal Security, Docker, Coinbase, Wizard World, Cisco, Airbnb (status, 2026-02-18)
