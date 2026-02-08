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
