---
name: job-hunting
description: Search and apply for remote software engineering jobs. Use when searching job boards, evaluating opportunities, preparing applications, or tracking job search progress. Handles company blocklists, salary requirements, and application customization.
---

# Job Hunting Skill

## Target Criteria

- **Location:** Remote only, **US-based ONLY**. **Never willing to relocate.**
  - ✅ "Remote (US)" or "Remote (United States)" — good
  - ✅ "Remote (North America)" — good (US is in North America)
  - ❌ "Remote (Canada)", "Remote (UK)", "Remote (EMEA)", etc. — **REJECT immediately**
  - If location says just "Remote" with no country, check the job description for residency requirements
- **Salary:** Top of range ≥ $200k (transparent salaries preferred)
- **Roles:** Infrastructure Engineer, Cloud Engineer, DevOps, SRE, Platform Engineer
- **Commitment:** Full-time

---

## ⚠️ CRITICAL REQUIREMENTS

### Browser Availability Check (FIRST THING)

**Workers connect via `agent-browser` CLI:**
```bash
agent-browser connect http://10.3.32.9:9222
```

If connection fails → **BLOCKED. IMMEDIATELY terminate** with:
```
BLOCKED: Cannot connect to browser at 10.3.32.9:9222. Cannot proceed.
```

**Do NOT attempt workarounds** (no web_fetch, no curl, no alternative approaches).

---

### ⚠️ ONE WORKER AT A TIME (MANDATORY PRE-FLIGHT)

**Before spawning ANY job application worker, the main agent MUST run:**

```
sessions_list activeMinutes=10
```

**Check:** Is there ANY session with label starting with `jobs.` that has `totalTokens > 0`?

- **If YES → DO NOT SPAWN.** Wait for the active worker to complete.
- **If NO → Safe to spawn ONE worker.**

**This is not optional.** Multiple browser workers clobber each other.

---

## Browser Connection Details

- **Headed Chromium** sidecar with Xvfb, VNC at `browser.lab.nkontur.com`
- **Always available** — runs 24/7
- **CDP endpoint:** `http://10.3.32.9:9222`
- **Resume path:** `/uploads/Resume (Kontur, Noah).pdf`
- **VNC:** noVNC at `https://browser.lab.nkontur.com/vnc.html` for visual debugging

**Worker commands (`agent-browser` via `exec` tool):**
```bash
agent-browser connect http://10.3.32.9:9222
agent-browser open <url>
agent-browser snapshot -i -c        # interactive-only, compact
agent-browser fill @ref "text"
agent-browser type @ref "text"       # character-by-character (human-like)
agent-browser click @ref
agent-browser hover @ref
agent-browser upload "input[type=file]" "/uploads/Resume (Kontur, Noah).pdf"
agent-browser eval "js expression"
agent-browser wait <ms>
agent-browser screenshot [path]
```

**All agents (scout and worker) use `agent-browser` CLI.**

---

## Hardened Scripts

**Location:** `skills/job-hunting/scripts/`

| Script | Usage | Purpose |
|--------|-------|---------|
| `detect-ats.sh` | `./scripts/detect-ats.sh <url>` | Returns: ashby\|greenhouse\|lever\|workday\|workable\|unknown |
| `check-applied.sh` | `./scripts/check-applied.sh <job-id>` | Exit 0 if not applied, exit 1 if already applied |
| `check-blocklist.sh` | `./scripts/check-blocklist.sh <company>` | Exit 0 if not blocked, exit 1 if blocked |
| `record-application.sh` | `./scripts/record-application.sh --id <id> --company <company> --role <role> [--url <url>] [--app-url <app-url>] [--salary <salary>] [--notes <notes>]` | Updates applied.json and tracker.md |
| `solve-captcha.sh` | `./scripts/solve-captcha.sh <hcaptcha\|recaptcha> <sitekey> <page_url>` | Solves CAPTCHA via 2captcha API (~10-30s) |

---

## Two-Tier Architecture: Main → Scout → Main → Workers

```
MAIN AGENT (conversation with user)
    ↓ spawns scout
SCOUT SUB-AGENT (searches, filters, returns list)
    ↓ reports findings back to main (does NOT apply)
MAIN AGENT 
    ↓ spawns workers ONE at a time based on scout's list
WORKER SUB-AGENT (does ONE job application)
    ↓ reports back to main
Main updates tracking, spawns next worker
```

**Why:** Sub-agents cannot spawn other sub-agents. Scout stays lightweight. Workers get fresh context per application.

### Main Agent Responsibilities

1. Spawn a **scout** to search and return a list of jobs
2. Wait for scout to report back
3. Spawn **worker** sub-agents ONE AT A TIME
4. Track results, update applied.json and tracker.md after each worker

**Spawn scout:**
```
sessions_spawn(
  label: "jobs.scout",
  task: "You are a job scout. Read the job-hunting skill. Search Hiring Cafe LIST VIEW ONLY for up to [N] jobs matching criteria. Return a structured list with: company, role, salary, job ID, URL. Do NOT open job pages or apply.",
  runTimeoutSeconds: 600
)
```

**Spawn workers (one at a time):**
```
sessions_spawn(
  label: "jobs.apply.<company>",
  task: "Apply to <Company> - <Role>. Job ID: <id>. URL: <url>. Read job-hunting skill, section WORKER INSTRUCTIONS.",
  runTimeoutSeconds: 1800
)
```

### Handling Worker Results

- **SUCCESS:** Update applied.json and tracker.md, spawn next worker
- **SKIPPED:** Log reason, spawn next worker
- **FAILED:** Log reason, decide retry or skip, spawn next worker
- **NEEDS_INPUT:** Ask user, relay via sessions_send or spawn fresh worker with answer

---

## Scout Instructions

**You are a scout if your task mentions "scout" or asks you to search/find jobs.**

```
╔══════════════════════════════════════════════════════════════════╗
║  SCOUT DOES NOT: fill forms, click Apply, open job pages,       ║
║  spawn workers, or update tracking files.                        ║
║  You search. You filter. You report. That's it.                  ║
╚══════════════════════════════════════════════════════════════════╝
```

**Scout uses `agent-browser` CLI**, same as workers.

**Workflow:**
1. Connect: `agent-browser connect http://10.3.32.9:9222`
2. Open Hiring Cafe search URL (see Browser Workflow section), LIST VIEW ONLY
2. Extract job titles, companies, salaries, URLs from the list
3. Filter against `references/blocklist.md`, `references/applied.json`, and `references/rejected.json`
4. **Max 10 jobs per search** — pick best 10 if more found
5. Report structured list:

```markdown
## Jobs Found

### 1. <Company> - <Role>
- **Salary:** $XXXk-$XXXk/yr
- **ID:** <job-id-from-url>
- **URL:** https://hiring.cafe/viewjob/<id>
- **Location:** Remote (US) ✅
```

Then **EXIT**.

---

## Quick Reference

- **Blocklist:** `references/blocklist.md`
- **Profile:** `references/profile.md`
- **Tracking:** `references/tracker.md`
- **Applied Jobs:** `references/applied.json`
- **Rejected Jobs:** `references/rejected.json`
- **Retry Queue:** `references/retry-queue.json`
- **Manual Queue:** `references/manual-queue.json`
- **Stories:** `references/stories.md`
- **Resume:** `/uploads/Resume (Kontur, Noah).pdf`
- **LinkedIn:** N/A — leave blank or enter "N/A"
- **Telegram:** Chat ID `8531859108`

## Voice & Persona

**When filling applications, BE Noah.** Write in first person. Never say "Noah is..." — say "I am..."

**Standard fields:**
- **Name:** Noah Kontur
- **Legal name:** Noah P. Kontur (middle INITIAL only)
- **Email:** konoahko@gmail.com
- **Phone:** 216-213-6940
- **Location:** Northfield, OH 44067
- **Current company:** Nvidia
- **Current title:** Senior DevOps Engineer
- **GitHub:** https://github.com/konturn
- **Website:** https://nkontur.com
- **LinkedIn:** N/A

**Date verification:** Before filling date fields, run `TZ=America/New_York date "+%Y-%m-%d"`. Do NOT assume dates.

**Writing style — SPEAK LIKE A HUMAN:**

**❌ Avoid:**
- "What draws me to X is the opportunity to..."
- "I'm particularly excited about..."
- "aligns perfectly with my experience"
- Any sentence that could appear on a motivational poster
- Em dashes (—) — use commas, periods, or "and"

**✅ Use:**
- Contractions (I'm, don't, can't)
- Short punchy sentences. Even fragments.
- Specifics over generics. Names, numbers, concrete details.
- Starting sentences with "And" or "But" occasionally

**The test:** Read it out loud. Would you say this at a coffee shop? If it sounds like a press release, rewrite it.

---

## Handling Optional Prompts

### Personal Stories
1. Check `references/stories.md` for existing stories
2. If a story fits → adapt it
3. **If no story fits → STOP and ask Noah**

### Answer Questions Exactly As Asked
"Describe one project" → ONE. "In 2-3 sentences" → stay in range. Follow instructions literally.

### "Interest in X" Questions
Lead with genuine personal connection, then connect to why the role appeals. Don't dodge with pure technical justification.

### "Why This Company?" Questions

**Every response must pass the "competitor test":** Could this answer apply to a different company in the same space? If yes, rewrite.

**REQUIRED:** At least ONE specific detail showing research (blog post, GitHub repo, product feature, talk, technical decision).

**Structure:** Hook (specific thing) → Bridge (your experience, prefer Nvidia/OctoAI) → Curiosity (what you'd dig into). 3-4 sentences.

### Salary Expectations
- **Single number:** $250,000
- **Range (if forced):** $225,000 - $250,000
- **Hourly:** $120/hr

### AI/Automation Disclosure
Answer **NO**. This is Noah's application.

### Cover Letters
3-4 paragraphs max. Lead with experience, reference specific requirements, end with genuine interest.

---

## Workflow

### 1. Search Jobs

Primary source: **Hiring Cafe** (hiring.cafe)

**Direct search URL:**
```
https://hiring.cafe/?searchState=%7B%22locations%22%3A%5B%7B%22formatted_address%22%3A%22United+States%22%2C%22types%22%3A%5B%22country%22%5D%2C%22geometry%22%3A%7B%22location%22%3A%7B%22lat%22%3A%2241.3284%22%2C%22lon%22%3A%22-81.4981%22%7D%7D%2C%22id%22%3A%22user_country%22%2C%22address_components%22%3A%5B%7B%22long_name%22%3A%22United+States%22%2C%22short_name%22%3A%22US%22%2C%22types%22%3A%5B%22country%22%5D%7D%5D%2C%22options%22%3A%7B%22flexible_regions%22%3A%5B%22anywhere_in_continent%22%2C%22anywhere_in_world%22%5D%7D%2C%22workplace_types%22%3A%5B%22Remote%22%5D%7D%5D%2C%22searchQuery%22%3A%22infrastructure+engineer+cloud+engineer%22%2C%22restrictJobsToTransparentSalaries%22%3Atrue%2C%22maxCompensationLowEnd%22%3A%22200000%22%2C%22hideJobTypes%22%3A%5B%22Applied%22%2C%22Viewed%22%5D%7D
```

Before presenting any job: check applied.json, blocklist.md, verify Remote US, confirm $200k+.

### 2. Evaluate Opportunity

Assess: tech stack alignment (AWS, Terraform, K8s, Python/Go), YOE requirements vs ~3-4 years, company stage, red flags.

### 3. Apply

1. Load `references/profile.md`
2. Fill standard fields
3. For optional story questions: check `references/stories.md`, ask Noah if none fit
4. Upload resume
5. Submit (pre-approved, no confirmation needed)
6. Update tracking files

### 4. Track

**After every application, update BOTH:**

1. **applied.json:**
```json
{
  "id": "<job-id>",
  "company": "Company Name",
  "role": "Job Title",
  "url": "https://hiring.cafe/viewjob/xxx",
  "applicationUrl": "https://jobs.ashbyhq.com/company/role-id",
  "salary": "$XXXk-$XXXk",
  "location": "Remote",
  "applied": "YYYY-MM-DD",
  "notes": ""
}
```

2. **tracker.md:** Company, role, date, status, notes.

---

## ⚡ Anti-Detection & Human Simulation

### Core Principles
1. No instant fills — vary timing between fields
2. Variable, not uniform delays — humans are inconsistent
3. Engage the page — scroll, hover, pause to read before filling
4. Target 3-7 min for simple forms, 8-15 for complex

### Timing Guidelines (vary ±30%)

| Action | Delay |
|--------|-------|
| Page load → first interaction | 3-8s |
| Between field fills | 1-4s |
| Before clicking Submit | 3-6s |
| After dropdown selection | 1-2s |

### Implementation

```bash
# Stealth patches — run on every new page load
agent-browser eval "Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); window.chrome = { runtime: {} }; Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] }); Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });"

# Human-like timing between fields
agent-browser fill @e3 "Noah"
agent-browser wait 2000
agent-browser fill @e4 "Kontur"
agent-browser wait 1500

# Hover-then-click on ALL buttons
agent-browser hover @e7
agent-browser wait 500
agent-browser click @e7

# Character-by-character typing for sensitive forms
agent-browser type @e3 "Noah"
```

---

## Resume Upload

```bash
agent-browser upload "input[type=file]" "/uploads/Resume (Kontur, Noah).pdf"
```

**Verify:** Snapshot and confirm filename "Resume (Kontur, Noah).pdf" appears.

```bash
agent-browser snapshot -i -c
# Look for "Resume" or "Kontur" in output
```

**If upload fails:** Retry once with alternative selector:
```bash
agent-browser upload "#resume-input" "/uploads/Resume (Kontur, Noah).pdf"
agent-browser upload "[name='resume']" "/uploads/Resume (Kontur, Noah).pdf"
```

**If still fails:** Report NEEDS_INPUT with form URL. Notify Noah:
```
message action=send channel=telegram target=8531859108 message="🚨 Resume upload failed for <Company>. Form ready at <URL> - please upload manually."
```

**An application is NOT successful unless the resume has been uploaded.**

---

## Worker Workflow

### Step-by-step

```bash
# 1. Connect to browser
agent-browser connect http://10.3.32.9:9222

# 2. Open application URL
agent-browser open "<application-url>"

# 3. Inject stealth patches
agent-browser eval "Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); window.chrome = { runtime: {} }; Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] }); Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });"

# 4. Wait and engage the page
agent-browser wait 5000
agent-browser snapshot -i -c
agent-browser hover @<some-element>
agent-browser wait 2000

# 5. Fill fields with human timing
agent-browser fill @<first-name> "Noah"
agent-browser wait 2000
agent-browser fill @<last-name> "Kontur"
agent-browser wait 1500
agent-browser fill @<email> "konoahko@gmail.com"
agent-browser wait 3000
agent-browser fill @<phone> "216-213-6940"
agent-browser wait 2000
agent-browser fill @<location> "Northfield, OH"
agent-browser wait 1500

# 6. Upload resume
agent-browser upload "input[type=file]" "/uploads/Resume (Kontur, Noah).pdf"

# 7. Fill dropdowns (hover-then-click)
agent-browser wait 2500
agent-browser hover @<dropdown>
agent-browser click @<dropdown>

# 8. Fill custom questions (research company first, use human voice)

# 9. Review form
agent-browser wait 4000
agent-browser snapshot -i -c
# Verify: resume uploaded, salary says $250,000, all required fields filled

# 10. Submit
agent-browser hover @<submit>
agent-browser wait 1000
agent-browser click @<submit>
```

### Worker Reporting

Report back with: `SUCCESS` / `SKIPPED` / `FAILED` / `NEEDS_INPUT`

**Always include open-ended responses:**
```
**Open-Ended Responses:**
Q: "Why are you interested in [Company]?"
A: "<what you wrote>"
```

Include any issues encountered and suggestions for the skill.

---

## CAPTCHA Handling

**Use 2captcha service to solve automatically.** API key stored in Vault at `homelab/data/agents/2captcha`.

### Automated Solving Flow

```bash
# 1. Detect the captcha type and sitekey from the page
SITEKEY=$(agent-browser eval "document.querySelector('[data-sitekey]')?.dataset?.sitekey || document.querySelector('iframe[src*=\"hcaptcha\"]')?.src?.match(/sitekey=([^&]+)/)?.[1] || document.querySelector('iframe[src*=\"recaptcha\"]')?.src?.match(/k=([^&]+)/)?.[1] || ''")

# 2. Determine type
# hCaptcha: iframe src contains "hcaptcha.com"
# reCAPTCHA: iframe src contains "recaptcha" or "google.com/recaptcha"
CAPTCHA_TYPE="hcaptcha"  # or "recaptcha"

# 3. Get the current page URL
PAGE_URL=$(agent-browser eval "window.location.href")

# 4. Solve it
TOKEN=$(./scripts/solve-captcha.sh "$CAPTCHA_TYPE" "$SITEKEY" "$PAGE_URL")

# 5. Inject the solution token
# For hCaptcha:
agent-browser eval "document.querySelector('[name=\"h-captcha-response\"]').value = '$TOKEN'; document.querySelector('[name=\"g-recaptcha-response\"]').value = '$TOKEN';"

# For reCAPTCHA:
agent-browser eval "document.querySelector('[name=\"g-recaptcha-response\"]').value = '$TOKEN';"

# 6. Some forms need a callback triggered
agent-browser eval "typeof hcaptcha !== 'undefined' && hcaptcha.getRespKey && document.querySelector('[data-callback]')?.dataset?.callback && window[document.querySelector('[data-callback]').dataset.callback]('$TOKEN');"
```

### Script Reference

| Script | Usage | Purpose |
|--------|-------|---------|
| `solve-captcha.sh` | `./scripts/solve-captcha.sh <hcaptcha\|recaptcha> <sitekey> <page_url>` | Returns solution token. ~10-30s. Costs ~$0.003/solve. |

### If 2captcha Fails

Add to `references/manual-queue.json` and notify Noah:
```
message action=send channel=telegram target=8531859108 message="🔐 CAPTCHA solving failed for <Company>. Form ready at <URL>."
```

---

## Email Verification / OTP Codes

### Handling Flow

1. **Recognize verification prompt** — "Enter the code sent to konoahko@gmail.com"
2. **Fetch code from Gmail via JIT (T1 auto-approve):**
   ```bash
   sleep 15
   source /home/node/.openclaw/workspace/tools/jit-lib.sh
   TOKEN=$(jit_gmail_token)  # T1 auto-approve, no approval tap needed
   
   # Search for recent verification emails (last 5 min)
   MSGS=$(curl -s -H "Authorization: Bearer $TOKEN" \
     "https://gmail.googleapis.com/gmail/v1/users/me/messages?maxResults=5&q=newer_than:5m+from:greenhouse-mail.io" \
     | jq -r '.messages[]?.id')
   
   # Fetch body and extract code
   for id in $MSGS; do
     curl -s -H "Authorization: Bearer $TOKEN" \
       "https://gmail.googleapis.com/gmail/v1/users/me/messages/$id?format=full" \
       | jq -r '.payload.body.data // .payload.parts[0].body.data' \
       | base64 -d 2>/dev/null | grep -oP '\b\d{6,8}\b' | head -1
   done
   ```
3. **Fallback:** Ask Noah via Telegram if auto-fetch fails after 60s
4. **Enter code** — For character-by-character inputs, use individual key presses

### Email Access

```bash
# Gmail API via JIT (T1 auto-approve for reads)
source /home/node/.openclaw/workspace/tools/jit-lib.sh
TOKEN=$(jit_gmail_token)

# Search emails
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://gmail.googleapis.com/gmail/v1/users/me/messages?maxResults=10&q=from:somecompany.com"

# Read email body
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://gmail.googleapis.com/gmail/v1/users/me/messages/MSG_ID?format=full"
```

---

## ATS-Specific Patterns

### General (All ATS)
1. Snapshot first — see all fields before filling
2. Fill ALL required fields manually
3. Verify resume filename visible before submitting
4. Verify salary says $250,000
5. Hover-then-click on all buttons

### Ashby (jobs.ashbyhq.com)
- **⚠️ Ashby has aggressive spam detection.** Do NOT skip — attempt the application.
- Use `agent-browser type` (character-by-character) for ALL fields — never `fill`
- Target 5-8 min minimum form completion time
- Scroll to bottom and back before filling any fields
- Inject stealth patches before any interaction
- If submission is blocked/flagged after filling, THEN report FAILED with details

### Greenhouse (boards.greenhouse.io)
- Fill all fields manually (First/Last/Email/Phone/Location/LinkedIn)
- May have reCAPTCHA — if blocked, notify Noah

### Lever (jobs.lever.co)
- "Additional information" section often needs custom cover letter
- "Couldn't auto-read your resume" message is **normal**, not an error

### Workday (myworkdayjobs.com)
- Multi-step forms — check each page
- Account credentials: `konoahko@gmail.com` / `jobApplications123@`
- **Never use Google OAuth** — always email/password
- For unresponsive buttons: hover-then-click

### Workable (apply.workable.com)
- Uses Cloudflare Turnstile CAPTCHA heavily
- If blocked, report NEEDS_INPUT with form URL

---

## ⚠️ OPEN-ENDED RESPONSES — DON'T PHONE IT IN

Every response must pass THREE tests:
1. **Competitor Test:** Could this work for a different company? If yes, too generic.
2. **Coffee Shop Test:** Would you say this to someone? If it sounds like a press release, rewrite.
3. **Proof Test:** At least ONE specific, verifiable detail showing research.

**Before writing ANY open-ended response, find at least ONE of:** a specific blog post, GitHub repo, product feature, talk, or technical detail from their docs.

**If you can't find anything specific, say so honestly** — that's better than fake enthusiasm.

---

## Resilience & Error Handling

### Retry Logic

Max 3 attempts, exponential backoff: 1s → 2s → 4s.

**Retryable:** Gateway timeout, connection reset, element not found.
**Non-retryable:** File not found, resume upload failed after retries (do NOT submit without resume).

### Application Checkpointing

Save progress to `references/in_progress/<jobId>.json` after major steps. On success, move to applied.json and delete checkpoint.

### Recovery Flow

1. Check `references/in_progress/` for incomplete applications
2. Snapshot form to assess state
3. Re-fill or continue from last checkpoint

---

## Worker Instructions

**You are a worker if your task mentions a specific company/role to apply to.**

1. **Connect browser:** `agent-browser connect http://10.3.32.9:9222` — if fails, report BLOCKED
2. **Read the skill** for voice guidance and standard fields
3. **Validate job criteria:** Open posting, verify salary ≥$200k and Remote US. If not → `SKIPPED: <reason>`
4. **Navigate** to application URL
5. **Inject stealth patches** (see Anti-Detection)
6. **Wait 5-8s**, scroll, hover (simulate reading)
7. **Snapshot:** `agent-browser snapshot -i -c`
8. **Fill ALL fields with human timing** (see Worker Workflow)
9. **Upload resume:** `agent-browser upload "input[type=file]" "/uploads/Resume (Kontur, Noah).pdf"`
10. **Fill custom questions** — research company, use human voice
11. **Verify:** resume uploaded, salary $250k, all fields filled
12. **Submit** (hover-then-click)
13. **Report:** SUCCESS / SKIPPED / FAILED / NEEDS_INPUT + open-ended responses + feedback

### Worker Constraints

Workers should **NOT:**
- Search for other jobs
- Modify applied.json (main agent does this)
- Apply to any job other than their assignment
- Send Telegram messages directly (report to main instead)

---

## Main Agent Dispatcher Instructions

### Spawning Workers

One at a time. ⚠️ **NEVER RUN WORKERS IN PARALLEL.**

### Handling Open-Ended Responses

Review quality of worker responses. If concerning, notify Noah:
```
message action=send channel=telegram target=8531859108 message="📝 Application Response Review..."
```

### Updating the Skill

You have permission to edit this skill based on worker feedback. Common improvements: new ATS patterns, better selectors, edge cases, clarified instructions.

---

## Nightly Retry System

Jobs that fail due to technical issues go into `references/retry-queue.json`.

### When to Add
- Resume upload failed but form otherwise ready
- Multi-step form timed out
- Technical failure that seems solvable differently

**CAPTCHA/spam blocks → `references/manual-queue.json` instead.**

### Retry Agent Instructions

You have context about what failed. Be creative — standard methods already failed.

**Strategies:** Different selectors, JS injection, coordinate-based clicking, wait longer, screenshot analysis, alternative UI paths.

### Retry Limits
- Max 5 attempts
- After 5: move to `references/abandoned.json`
