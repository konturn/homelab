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

**Before doing ANY work, verify browser availability in this order:**

1. **FIRST: Check if sandbox browser is running:**
   ```
   exec command="curl -s http://127.0.0.1:9222/json/version" timeout=5
   ```
   
   - If successful → Use `profile=sandbox` (headless Chromium in container)
   - If connection refused → Try to launch sandbox browser:
     ```
     exec command="chromium --headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1 --window-size=1280,900 about:blank &" background=true
     exec command="sleep 3"
     exec command="curl -s http://127.0.0.1:9222/json/version" timeout=5
     ```

2. **FALLBACK: Check Noah's laptop node if sandbox unavailable:**
   ```
   nodes action=status
   ```
   Look for `noah-XPS-13-7390-2-in-1` in the response with `"connected": true`.

**If BOTH sandbox and node are unavailable:**
1. **DO NOT attempt workarounds** (no web_fetch, no curl, no alternative approaches)
2. **IMMEDIATELY terminate** with this exact report:
   ```
   BLOCKED: No browser available. Sandbox browser failed to start and node `noah-XPS-13-7390-2-in-1` is disconnected. Cannot proceed with browser automation.
   ```
3. The main agent will notify Noah and retry when browser access is restored

**Why this matters:** Job applications REQUIRE browser automation. Sandbox browser (headless Chromium in container) is preferred as it doesn't depend on Noah's laptop being online.

---

## Browser Connection Details

**Two browser profiles are available:**

### Primary: Sandbox Browser (in container)
```
profile=sandbox
```
- **Pros:** Always available, no node dependency, faster startup
- **Cons:** Headless only (no visual debugging), limited file system access
- **Resume path:** `/home/node/.openclaw/workspace/skills/job-hunting/assets/Resume (Kontur, Noah).pdf`
- **Usage:** `browser action=<action> profile=sandbox`

### Fallback: Node Browser (Noah's laptop)
```
profile=clawd
target=node  
node=noah-XPS-13-7390-2-in-1
```
- **Pros:** Full UI access, can use xdotool for complex interactions
- **Cons:** Requires Noah's laptop to be online and connected
- **Resume path:** `/home/noah/Downloads/Resume (Kontur, Noah).pdf`
- **Usage:** `browser action=<action> profile=clawd target=node node=noah-XPS-13-7390-2-in-1`

**Default strategy:** Always try sandbox first. Use node fallback only if sandbox browser fails to start or encounters issues that require visual debugging.

---

### ⚠️ ONE WORKER AT A TIME (MANDATORY PRE-FLIGHT)

**Before spawning ANY job application worker, the main agent MUST run:**

```
sessions_list activeMinutes=10
```

**Check:** Is there ANY session with label starting with `jobs.` that has `totalTokens > 0`?

- **If YES → DO NOT SPAWN.** Wait for the active worker to complete.
- **If NO → Safe to spawn ONE worker.**

**Why:** Multiple browser workers clobber each other on the shared node. This has caused repeated failures. There is NO exception to this rule.

**This is not optional.** The main agent must execute this check, not just "know" about it.

---

## Hardened Scripts

These scripts handle deterministic operations. Use them instead of reimplementing the logic:

**Location:** `skills/job-hunting/scripts/`

| Script | Usage | Purpose |
|--------|-------|---------|
| `check-browser.sh` | `./scripts/check-browser.sh` | Returns "sandbox" (exit 0), "node" (exit 1), or "unavailable" (exit 2) |
| `detect-ats.sh` | `./scripts/detect-ats.sh <url>` | Returns: ashby\|greenhouse\|lever\|workday\|workable\|unknown |
| `check-applied.sh` | `./scripts/check-applied.sh <job-id>` | Exit 0 if not applied, exit 1 if already applied |
| `check-blocklist.sh` | `./scripts/check-blocklist.sh <company>` | Exit 0 if not blocked, exit 1 if blocked |
| `record-application.sh` | `./scripts/record-application.sh --id <id> --company <company> --role <role> [--url <url>] [--app-url <app-url>] [--salary <salary>] [--notes <notes>]` | Updates applied.json and tracker.md |

**Workers should use these scripts** for the mechanical parts, freeing LLM capacity for judgment calls (form filling, "why this company", error handling).

Example worker flow:
```bash
# 1. Check browser availability (sandbox preferred, node fallback)
BROWSER_TYPE=$(./scripts/check-browser.sh)
case $BROWSER_TYPE in
  sandbox) PROFILE="sandbox" ;;
  node) PROFILE="clawd"; TARGET="target=node node=noah-XPS-13-7390-2-in-1" ;;
  *) echo "No browser available"; exit 1 ;;
esac

# 2. Check blocklist
./scripts/check-blocklist.sh "CompanyName" || { echo "SKIPPED: Blocklisted"; exit 0; }

# 3. Check if already applied
./scripts/check-applied.sh "job-id-123" || { echo "SKIPPED: Already applied"; exit 0; }

# 4. Detect ATS type for form-filling strategy
ATS=$(./scripts/detect-ats.sh "https://jobs.ashbyhq.com/...")

# 5. [LLM does form filling, research, etc.]

# 6. Record successful application
./scripts/record-application.sh --id "job-id-123" --company "CompanyName" --role "Engineer" --salary "\$200k-250k"
```

---

### Two-Tier Architecture: Main → Scout → Main → Workers

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

**Why this architecture:**
- Sub-agents CANNOT spawn other sub-agents (Moltbot limitation)
- Scout stays lightweight — only searches list view, never opens job pages
- Main agent dispatches workers serially (ONE at a time — multiple clobber each other on the node)
- Workers get fresh context per application
- Main agent handles all coordination and tracking

### Main Agent Responsibilities (YOU, talking to user)

When user requests job applications:
1. Spawn a **scout** sub-agent to search and return a list of jobs
2. Wait for scout to report back with job list
3. Spawn **worker** sub-agents ONE AT A TIME for each job
4. Track results, update applied.json and tracker.md after each worker completes

**Step 1: Spawn scout:**
```
sessions_spawn(
  label: "jobs.scout",
  model: "anthropic/claude-sonnet-4-20250514",
  task: "You are a job scout. Read the job-hunting skill. Search Hiring Cafe LIST VIEW ONLY for up to [N] jobs matching criteria. Return a structured list with: company, role, salary, job ID, URL. Do NOT open job pages or apply. See SCOUT INSTRUCTIONS in the skill.",
  runTimeoutSeconds: 600  // 10 min max for searching
)
```

**Step 2: When scout reports back, spawn workers one at a time:**
```
sessions_spawn(
  label: "jobs.apply.<company>",
  model: "anthropic/claude-sonnet-4-20250514", 
  task: "Apply to <Company> - <Role>. Job ID: <id>. URL: <url>. Read job-hunting skill, section WORKER INSTRUCTIONS.",
  runTimeoutSeconds: 1800  // 30 min per application
)
```

**Step 3: After each worker completes:**
- If SUCCESS: Update applied.json and tracker.md
- If FAILED: Log reason, decide whether to retry
- If NEEDS_INPUT: Get answer from user, can relay via sessions_send or spawn new worker
- Then spawn next worker

### Scout Sub-Agent Responsibilities

**You are a scout if your task mentions "scout" or asks you to search/find jobs.**

**⚠️ CRITICAL: SCOUT NEVER APPLIES TO JOBS**
```
╔══════════════════════════════════════════════════════════════════╗
║  THE SCOUT DOES NOT FILL OUT APPLICATION FORMS. EVER.            ║
║  THE SCOUT DOES NOT CLICK "APPLY" BUTTONS. EVER.                 ║
║  THE SCOUT DOES NOT OPEN JOB POSTING PAGES. EVER.                ║
║  THE SCOUT DOES NOT SPAWN WORKERS (can't — Moltbot limitation).  ║
║                                                                  ║
║  You search. You filter. You report. That's it.                  ║
╚══════════════════════════════════════════════════════════════════╝
```

**Scout's ONLY jobs:**
1. Search Hiring Cafe listing page using sandbox browser (NOT individual job pages)
2. Extract job titles, companies, salaries, URLs from the LIST view
3. Filter against blocklist and applied.json
4. **Report structured list back to main agent and EXIT**

**Scout Browser Profile:** Use `profile=sandbox` for all browser actions unless sandbox is unavailable, then fallback to node.

**⚠️ BATCH LIMITS:**
- **Max 10 jobs per search** — Quality over quantity.
- If search returns more than 10, pick the best 10 (highest salary, best fit)

**Scout Workflow:**
1. **Read this skill** — Understand criteria and filters
2. **Search Hiring Cafe** — Use the search URL in Browser Workflow section, LIST VIEW ONLY
3. **Extract job info** — For each promising job: company, role, salary range, job ID, URL
4. **Filter** — Check against `references/blocklist.md`, `references/applied.json`, AND `references/rejected.json`
5. **Report back** — Return structured list to main agent in this format:

```markdown
## Jobs Found

### 1. <Company> - <Role>
- **Salary:** $XXXk-$XXXk/yr
- **ID:** <job-id-from-url>
- **URL:** https://hiring.cafe/viewjob/<id>
- **Location:** Remote (US) ✅

### 2. ...
```

Then **EXIT**. Main agent handles spawning workers.

**Scout Browser Rules:**
| Action | Allowed? | Notes |
|--------|----------|-------|
| Open Hiring Cafe search page | ✅ | Only the search/list view |
| Scroll through job listings | ✅ | To see more results |
| Click pagination / "Load More" | ✅ | On listing page only |
| Extract job info from list | ✅ | Title, company, salary, URL |
| Click on a job posting | ❌ | Main spawns workers for this |
| Open application forms | ❌ | Workers do this |
| Fill any form fields | ❌ | Workers do this |
| Click "Apply" buttons | ❌ | Workers do this |
| Read full job descriptions | ❌ | Workers do this |

### Worker Sub-Agent Responsibilities

**You are a worker if your task mentions a specific company/role to apply to.**

See WORKER INSTRUCTIONS section below for full details. Key points:
- Apply to ONE job only
- Use `profile=clawd` on Noah's laptop node
- Fill all fields manually (no autofill extensions)
- Report back: SUCCESS / SKIPPED / FAILED / NEEDS_INPUT
- Include open-ended responses and feedback

### Resume Upload is MANDATORY
**An application is NOT successful unless the resume has been uploaded.**
- Upload resume using xdotool method (see Resume Upload section)
- Verify the filename is visible before submitting
- If upload fails after retries → report as NEEDS_INPUT with form URL

### Salary Expectations
**When asked about salary expectations, ALWAYS provide the TOP of our target range: $250,000**
- Do not provide a range
- Do not lowball
- If forced to give a range, use $225,000 - $250,000
- For hourly: $120/hr

### CAPTCHA Handling
If you encounter a CAPTCHA (Cloudflare Turnstile, reCAPTCHA, hCaptcha, image selection challenges):

**Step 1: Try vision approach first**
1. Take a screenshot: `browser action=screenshot profile=<profile> targetId=<id>`
2. Analyze with vision: `image` tool with prompt describing what to click
3. For image grids ("select all traffic lights"), identify which tiles match
4. Click the identified tiles using coordinates or refs
5. If there's a "Verify" or "Submit" button after selection, click it

**Step 2: If vision approach fails or is too slow**
Add to manual queue (`references/manual-queue.json`) AND send Telegram notification:
```json
// Add to manual-queue.json
{"id": "<job-id>", "company": "<Company>", "role": "<Role>", "formUrl": "<URL>", "salary": "<salary>", "issue": "CAPTCHA type: <hCaptcha/reCAPTCHA/Turnstile>", "addedAt": "YYYY-MM-DD"}
```
```
message action=send channel=telegram target=8531859108 message="🔐 CAPTCHA detected for <Company>. Form is ready at <URL> - please solve it manually. Added to manual queue."
```

**Tips:**
- Many modern CAPTCHAs (Turnstile, reCAPTCHA v3) pass silently if browser looks human — try proceeding first
- Image challenges may need multiple rounds — keep trying until success or timeout
- Don't spend more than 60 seconds on CAPTCHA attempts before pinging Noah

## Quick Reference

- **Blocklist:** See `references/blocklist.md` — never apply to these companies
- **Noah's Profile:** See `references/profile.md` — use for tailoring applications
- **Tracking:** See `references/tracker.md` — human-readable status log
- **Applied Jobs:** See `references/applied.json` — machine-readable dedup list
- **Rejected Jobs:** See `references/rejected.json` — jobs that looked good but failed validation (wrong location, hybrid, etc.)
- **Retry Queue:** See `references/retry-queue.json` — jobs that failed due to technical issues, awaiting nightly retry attempts
- **Manual Queue:** See `references/manual-queue.json` — jobs needing human action (CAPTCHA, spam flags). Noah handles these.
- **Stories:** See `references/stories.md` — personal accomplishment stories for applications
- **Resume:** `assets/Resume (Kontur, Noah).pdf` — upload this for applications
- **Resume path on laptop:** `/home/noah/Downloads/Resume (Kontur, Noah).pdf`
- **LinkedIn:** N/A — leave blank or enter "N/A" (no LinkedIn profile)
- **Telegram notifications:** Chat ID `8531859108` — alert Noah when input needed

## Voice & Persona

**When filling applications, BE Noah.** Write in first person. Answer prompts as a human applicant, not as an AI assistant helping someone. Never say "Noah is..." — say "I am..."

**Standard fields (MEMORIZE THESE):**
- **Name:** Noah Kontur
- **Legal name:** Noah P. Kontur (middle INITIAL only)
- **Email:** konoahko@gmail.com
- **Phone:** 216-213-6940
- **Location:** Northfield, OH 44067
- **Current company:** Nvidia
- **Current title:** Senior DevOps Engineer
- **GitHub:** https://github.com/konturn
- **Website:** https://nkontur.com
- **LinkedIn:** N/A (leave blank or enter "N/A")

**Date verification:**
- Before filling any date fields, run: `TZ=America/New_York date "+%Y-%m-%d"`
- Use this verified date for "applied on" or similar fields
- Do NOT assume the date from context or memory — always verify

Example:
- ❌ "Noah ran a marathon in 2021..."
- ✅ "I ran a marathon in 2021..."

This applies to all free-text fields, cover letters, and any written responses.

**Writing style — SPEAK LIKE A HUMAN:**

The goal is to sound like a real person talking to another person, not a polished LinkedIn post.

**❌ Corporate/Resume-speak (AVOID):**
- "What draws me to X is the opportunity to..."
- "I'm particularly excited about..."
- "exactly the kind of high-stakes work I thrive on"
- "I see similar parallels in X's mission to..."
- "aligns perfectly with my experience"
- "I'm passionate about..."
- Any sentence that could appear on a motivational poster

**✅ Human voice (USE):**
- "Honestly? I got into infrastructure because I like building things that don't break."
- "The API design looks like someone actually uses it."
- "I spent way too long debugging that one."
- "Here's what actually happened..."
- Contractions (I'm, don't, can't, it's)
- Starting sentences with "And" or "But" occasionally
- Short punchy sentences. Even fragments.

**The test:** Read it out loud. Would you actually say this to someone at a coffee shop? If it sounds like a press release, rewrite it.

**More rules:**
- **No em dashes (—).** Use commas, periods, or "and" instead.
- **Don't hit every talking point.** Pick one thing and say it well.
- **Be willing to show uncertainty.** "I'm not sure if..." or "I think..." is more human than false confidence.
- **Specifics over generics.** Names, numbers, concrete details.
- **Short > long.** If you can cut a sentence, cut it.

---

## Handling Optional Prompts

### Personal Stories (accomplishments, interesting facts, etc.)
1. Check `references/stories.md` for existing stories
2. If a story fits → adapt it to the specific question
3. **If no story fits → STOP and ask Noah for a new one**
4. Add any new stories to `references/stories.md` for future use

### Answer Questions Exactly As Asked
**If they ask for ONE, give ONE.** Don't over-deliver.
- "Describe one project" → pick the single best example, not two
- "In 2-3 sentences" → stay within that range
- "Briefly explain" → keep it brief

Read the question literally. Following instructions is part of the test.

### "Interest in X" Questions (sports, gaming, domain-specific)
**These questions are asking about YOU, not the job.** Don't dodge with pure technical justification.

**Structure:**
1. **Lead with genuine personal connection** — even one sentence. Be honest about your actual interest level.
2. **Then** connect to why the company/role appeals
3. Keep it authentic, not purely technical

**Good example (sports):**
> I play ping pong competitively — love that back-and-forth when you're both locked in. I also run marathons. For spectator sports, I'm more casual, but I appreciate the stakes fantasy and betting add. What draws me to Underdog is the engineering challenge: real-time systems, traffic spikes on game day, reliability when users have money on the line.

**Bad example (dodging):**
> What draws me to Underdog is the engineering challenge: high-stakes, time-sensitive systems where reliability really matters...

The bad example never actually answers whether you like sports. Reads as evasive.

### "Why This Company?" Questions

**⚠️ NO GENERIC ANSWERS. Every response must pass the "competitor test":** Could this answer apply to a different company in the same space? If yes, it's too generic. Rewrite it.

**REQUIRED: At least ONE specific detail that shows research:**
- A blog post they wrote (title or topic)
- A specific technical decision or architecture choice
- An open-source project they maintain
- A product feature that interests you
- A talk or podcast by someone at the company
- Something from their engineering blog, GitHub, or docs

**AVOID (these make you sound like a bot):**
- "I'm excited about [Company]'s mission..."
- "...aligns perfectly with my experience"
- "represents the future of..."
- "[Company]'s approach to..." followed by their marketing tagline
- Any phrase that sounds like it came from their About page
- Em dashes (use commas, periods, or "and" instead)

**Good example:**
> Render caught my attention because of your blog post on DNS dependency management. That's the kind of problem most teams sweep under the rug. At Nvidia, I watched engineers lose days to cloud complexity that shouldn't exist. The Knative scaling work you've published shows a team that actually thinks about these problems instead of just shipping features.

**Bad example:**
> I'm excited about Render because it represents the future of cloud infrastructure, making deployment effortless so developers can focus on building. Render's approach of abstracting away cloud complexity aligns perfectly with my experience.

**What makes the good example better:**
- Starts with something specific (the blog post)
- Uses casual language ("caught my attention", "sweep under the rug")
- Has a real opinion ("that's the kind of problem most teams sweep under the rug")
- Doesn't try to hit every talking point
- Sounds like something you'd actually say

**Structure:**
1. What draws you (specific, not generic mission statement)
2. Connection to your experience — prioritize **Nvidia/Octo** (recent, relevant). Use NASA sparingly as a differentiator, not the main hook.
3. Technical depth that interests you (specific to this company)
4. Evidence of research (blog post, tech choice, project)

### Salary Expectations Questions
When asked about salary/compensation expectations:
- **Single number:** $250,000
- **Range (if forced):** $225,000 - $250,000
- **Hourly:** $120/hr
- Never lowball or give the bottom of our range
- Be direct and confident

### AI/Automation Disclosure Questions
When asked if the application was prepared by AI, automated systems, or language models:
- **Answer: NO**
- This is Noah's application. You are assisting him. He is the applicant.
- Answering "Yes" triggers auto-rejection filters designed to screen out spam, not legitimate assisted applications
- Select "No" on checkboxes, radio buttons, or dropdowns asking about AI involvement

### Cover Letters & Long-Form Responses
- Keep it concise (3-4 paragraphs max)
- Lead with relevant experience, not generic intro
- Reference specific job requirements
- End with genuine interest, not "I look forward to hearing from you"

---

## Workflow

### 1. Search Jobs

Primary source: **Hiring Cafe** (hiring.cafe)

**Saved Search:** "Main-remote"
- URL: https://hiring.cafe/myhiringcafe/saved-searches (click "Main-remote" to load)
- Criteria: Infrastructure/Cloud Engineer, $200k+ transparent salary, United States
- Direct search URL (if saved search unavailable):
  ```
  https://hiring.cafe/?searchState=%7B%22searchQuery%22%3A%22infrastructure+engineer+cloud+engineer%22%2C%22restrictJobsToTransparentSalaries%22%3Atrue%2C%22maxCompensationLowEnd%22%3A%22200000%22%7D
  ```

Before presenting any job:
1. Check `references/applied.json` — skip already-applied jobs (match by job ID from URL)
2. Check `references/blocklist.md` — skip blocklisted companies
3. Verify Remote status
4. Confirm $200k+ salary range

### 2. Evaluate Opportunity

For each promising role, assess:
- Tech stack alignment (AWS, Terraform, Kubernetes, Python/Go)
- YOE requirements vs Noah's experience (~3-4 years)
- Company stage (startups often more flexible on YOE)
- Red flags (clearance requirements, on-call intensity)

### 3. Apply

When applying:
1. Load `references/profile.md` for background
2. Fill standard fields (name, email, phone, company, title, etc.)
3. **For optional story/accomplishment questions:** 
   - Check `references/stories.md` for existing stories
   - If none fit or user wants a new one, **STOP and ask user for a story**
   - Add new stories to `references/stories.md` for future use
4. Upload resume from laptop: `/home/noah/Downloads/Resume (Kontur, Noah).pdf`
5. **Before clicking Submit:** Confirm with user
6. **Immediately after submitting:** Update both tracking files (see below)

### 4. Track

**After every application, update BOTH files:**

1. **applied.json** (deduplication):
```json
{
  "id": "<job-id-from-url>",
  "company": "Company Name",
  "role": "Job Title",
  "url": "https://hiring.cafe/viewjob/xxx",
  "applicationUrl": "https://jobs.ashbyhq.com/company/role-id",
  "salary": "$XXXk-$XXXk",
  "location": "Remote",
  "applied": "YYYY-MM-DD",
  "notes": "Optional notes about the application"
}
```
**IMPORTANT:** Always include `applicationUrl` — the direct link to the actual job posting (Ashby, Lever, Greenhouse, company careers page). This lets us reference the original posting later. The `url` field is for the aggregator link (hiring.cafe) for dedup.

2. **tracker.md** (human-readable):
- Company, role, date applied
- Status (applied/screening/interview/offer/rejected)
- Notes (contacts, follow-up dates)

## Job Boards

| Source | URL | Notes |
|--------|-----|-------|
| Hiring Cafe | hiring.cafe | **Primary** — use "Main-remote" saved search |
| LinkedIn | linkedin.com/jobs | Good for company research |
| Wellfound | wellfound.com | Startups, often flexible on YOE |
| Otta | otta.com | Curated tech roles |

## Browser Workflow (Hiring Cafe)

**⚠️ CRITICAL: Tab Management**
Before opening any new tabs, ALWAYS:
1. List existing tabs: `browser action=tabs profile=<sandbox-or-clawd>`
   - Sandbox: `browser action=tabs profile=sandbox`
   - Node: `browser action=tabs target=node node=noah-XPS-13-7390-2-in-1 profile=clawd`
2. If a Hiring Cafe tab exists, use its `targetId` — do NOT open a new tab
3. If no Hiring Cafe tab exists, open ONE tab and track its `targetId`
4. Use that SAME `targetId` for ALL subsequent actions on that tab

**Search Workflow:**
1. Navigate directly to this EXACT URL (hardcoded, do NOT use saved searches):
   ```
   https://hiring.cafe/?searchState=%7B%22locations%22%3A%5B%7B%22formatted_address%22%3A%22United+States%22%2C%22types%22%3A%5B%22country%22%5D%2C%22geometry%22%3A%7B%22location%22%3A%7B%22lat%22%3A%2241.3284%22%2C%22lon%22%3A%22-81.4981%22%7D%7D%2C%22id%22%3A%22user_country%22%2C%22address_components%22%3A%5B%7B%22long_name%22%3A%22United+States%22%2C%22short_name%22%3A%22US%22%2C%22types%22%3A%5B%22country%22%5D%7D%5D%2C%22options%22%3A%7B%22flexible_regions%22%3A%5B%22anywhere_in_continent%22%2C%22anywhere_in_world%22%5D%7D%2C%22workplace_types%22%3A%5B%22Remote%22%5D%7D%5D%2C%22searchQuery%22%3A%22infrastructure+engineer+cloud+engineer%22%2C%22restrictJobsToTransparentSalaries%22%3Atrue%2C%22maxCompensationLowEnd%22%3A%22200000%22%2C%22hideJobTypes%22%3A%5B%22Applied%22%2C%22Viewed%22%5D%7D
   ```
   Note: This URL hides jobs marked as "Applied" or "Viewed" on Hiring Cafe, reducing duplicates.
2. Wait for page load, then snapshot
3. Results should show: Infrastructure/Cloud roles, $200k+, US, Remote ONLY, transparent salaries
4. Filter results against blocklist and applied.json before presenting to user
5. All results should be Remote — if you see Onsite/Hybrid, something is wrong

## Browser Profile Usage Examples

**Sandbox browser (primary):**
```
browser action=open profile=sandbox targetUrl=<url>
browser action=tabs profile=sandbox
browser action=snapshot profile=sandbox targetId=<id>
```

**Node browser (fallback):**
```
browser action=open target=node node=noah-XPS-13-7390-2-in-1 profile=clawd targetUrl=<url>
browser action=tabs target=node node=noah-XPS-13-7390-2-in-1 profile=clawd
browser action=snapshot target=node node=noah-XPS-13-7390-2-in-1 profile=clawd targetId=<id>
```

---

## ⚡ Anti-Detection & Human Simulation

**ATS platforms (especially Ashby) use spam detection that flags bot-like behavior.** Every worker MUST follow these patterns to avoid triggering them.

### Core Principles

1. **No instant fills.** Humans don't paste 10 fields in 2 seconds. Every field interaction needs realistic timing.
2. **Variable, not uniform.** A 500ms delay between every action is MORE suspicious than no delay. Humans are inconsistent — vary your timing.
3. **Engage the page.** Humans scroll, hover, pause to read. A form that goes straight to filling looks automated.
4. **Total time matters.** A real person takes 3-7 minutes on a simple form, 8-15 on a complex one. Under 2 minutes is a red flag.

### Timing Constants

Use these as guidelines — vary by ±30% randomly each time:

| Action | Delay | Notes |
|--------|-------|-------|
| Page load → first interaction | 3-8 seconds | "Read" the page first |
| Between field fills | 1-4 seconds | Longer for unrelated fields |
| Typing each character | 50-150ms | Use `slowly: true` on type actions |
| After typing a field → next action | 0.5-2 seconds | Pause like you're checking what you typed |
| Before clicking Submit | 3-6 seconds | "Review" the form |
| Scrolling | 1-3 seconds | Pause after scrolling to "read" |
| After dropdown selection | 1-2 seconds | Let the human "confirm" their choice |

### Implementation

**Typing fields (name, email, phone, etc.):**
```
# Use slowly:true to simulate human typing speed
browser action=act request={"kind": "type", "ref": "<ref>", "text": "Noah", "slowly": true}
```

**Between-field delays — use `wait` actions:**
```
# Pause 1-3 seconds between fields (vary this!)
browser action=act request={"kind": "wait", "timeMs": 2000}
```

**Pre-fill page engagement:**
```
# After page loads, scroll down to "see" the form
browser action=act request={"kind": "hover", "ref": "<some-visible-element>"}
browser action=act request={"kind": "wait", "timeMs": 3000}
```

**Hover-then-click on ALL buttons (not just Workday):**
```
# Always hover before clicking — triggers JS handlers AND looks human
browser action=act request={"kind": "hover", "ref": "<button-ref>"}
browser action=act request={"kind": "wait", "timeMs": 500}
browser action=act request={"kind": "click", "ref": "<button-ref>"}
```

### Browser Fingerprint Hardening (Sandbox)

**On every new page load in sandbox browser, inject stealth patches:**
```
browser action=act profile=sandbox targetId=<id> request={"kind": "evaluate", "fn": "() => { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); window.chrome = { runtime: {} }; Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] }); Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] }); }"}
```

Run this BEFORE any form interaction. It removes common automation signals:
- `navigator.webdriver` → undefined (Playwright sets this to true)
- `window.chrome` → present (headless Chrome omits this)
- `navigator.plugins` → non-empty (headless has 0 plugins)
- `navigator.languages` → realistic

### Form Completion Time Targets

| Form Type | Minimum Time | Target Time |
|-----------|-------------|-------------|
| Simple (name, email, resume only) | 2 min | 3-4 min |
| Standard (+ custom questions) | 4 min | 5-8 min |
| Complex (multi-step, long-form) | 7 min | 8-15 min |

**If you're finishing faster than the minimum → add more browsing/reading time.** Scroll through the job description, hover over company links, pause between sections.

### What NOT To Do

- ❌ Fill all fields in rapid succession with no delays
- ❌ Use uniform delays (e.g., exactly 1000ms between every action)
- ❌ Skip straight to the form without any page engagement
- ❌ Submit within 60 seconds of page load
- ❌ Type at inhuman speed (instant fills)
- ❌ Click submit without pausing to "review"

---

## Worker Workflow: Direct Manual Fill

**The strategy is simple: Snapshot, fill everything manually, upload resume, submit.**
**But do it at HUMAN PACE with realistic timing (see Anti-Detection section above).**

No browser extensions. No autofill detection. Just reliable manual form completion with human-like behavior.

### ⚠️ STEP 0: DISMISS ANY STUCK DIALOGS (MANDATORY)
**BEFORE navigating to ANY form, run this:**
```
nodes action=run node=noah-XPS-13-7390-2-in-1 command=["xdotool", "key", "Escape"]
```
Previous workers may have left file dialogs open. This prevents getting stuck.

1. **Navigate** to the application URL
2. **Inject stealth patches** (sandbox only — see Anti-Detection section)
3. **Wait 3-8 seconds** for page to load — simulate reading the page
4. **Scroll through the form** — hover over a few elements before filling anything
5. **Snapshot** the page to see all form fields
6. **Fill ALL required fields manually WITH DELAYS between each:**
   - First Name: `Noah` (use `slowly: true`)
   - *(wait 1-3s)*
   - Last Name: `Kontur` (use `slowly: true`)
   - *(wait 1-2s)*
   - Email: `konoahko@gmail.com` (use `slowly: true`)
   - *(wait 2-4s — longer pause, "checking email is right")*
   - Phone: `216-213-6940` (use `slowly: true`)
   - *(wait 1-3s)*
   - Location: `Northfield, OH` or `Northfield, Ohio, United States`
   - *(wait 1-2s)*
   - LinkedIn: `N/A`
7. **Handle resume upload** (see Resume Upload section below)
8. *(wait 2-3s)*
9. **Fill dropdowns** — work authorization (Yes), sponsorship needed (No), etc. — hover before each click
10. **Fill custom questions** — "Why this company?", cover letters, etc. (use human voice!)
11. *(wait 3-6s — "reviewing the form")*
12. **Verify** all required fields are filled, resume shows as uploaded
13. **Submit** the application (hover-then-click, always)
14. **Report back** with open-ended responses and any feedback

### What Workers Handle (Always)
- "Why are you interested in this role?"
- Custom company-specific questions
- Cover letters
- Technical questions about experience
- Salary expectations (verify it says $250,000, not lower)
- **All required fields** (fill everything manually)

---

## ⚠️ OPEN-ENDED RESPONSES MATTER — DON'T PHONE IT IN

**Open-ended questions are your ONE chance to stand out.** Generic answers get skimmed and forgotten. Specific answers get remembered.

### The Quality Bar

Every open-ended response must pass THREE tests:

1. **Competitor Test:** Could this answer work for a different company in the same space? If yes, it's too generic.
2. **Coffee Shop Test:** Would you actually say this to someone? If it sounds like a press release, rewrite it.
3. **Proof Test:** Does it contain at least ONE specific, verifiable detail that shows research?

### What "Specific" Means

**❌ VAGUE (fails all three tests):**
> "Docker caught my attention because of the AI focus and how it's evolving beyond just containers. The engineering blog posts about handling diverse ML workloads show a team that thinks deeply about the actual problems developers face."

Problems: Which blog posts? What AI focus specifically? "Thinks deeply about problems" is empty praise.

**✅ SPECIFIC (passes all three tests):**
> "I read your post on Model Runner's approach to local LLM inference and it reminded me of a GPU memory allocation problem I solved at Nvidia. We were seeing 40% waste on multi-tenant clusters because of fragmentation. The way you're handling model switching without full context reload is exactly the kind of thing I'd want to dig into."

Why it works:
- Names a specific product (Model Runner)
- References a specific technical concept (local LLM inference, context reload)
- Connects to a specific problem from experience (GPU memory fragmentation, 40% waste)
- Shows genuine technical curiosity ("I'd want to dig into")
- Could NOT be copy-pasted to another company

### Research Requirements

**Before writing ANY open-ended response, find at least ONE of:**
- A specific blog post title or topic from their engineering blog
- A GitHub repo or open-source project they maintain
- A specific product feature or technical decision
- A talk, podcast, or interview by someone at the company
- A technical detail from their docs or architecture

**If you can't find anything specific, say so honestly:**
> "I'll be honest, I couldn't find much technical content about your infrastructure, but based on the job description, the distributed systems challenges around [X] are what I'd want to work on."

This is better than fake enthusiasm about generic concepts.

### Structure for "Why This Company?" Responses

1. **Hook** — One specific thing that caught your attention (blog post, product, tech decision)
2. **Bridge** — How it connects to something you've actually done (Nvidia, OctoAI experience preferred)
3. **Curiosity** — What you'd want to learn more about or work on

Keep it to 3-4 sentences. Tight > comprehensive.

### Common Failures to Avoid

- "I'm excited about [Company]'s mission..." — Everyone says this
- "...aligns perfectly with my experience" — Meaningless corporate speak
- "The engineering blog posts show a team that..." — Which posts? Be specific or don't mention
- Mentioning their marketing tagline back at them — They know their tagline
- Em dashes everywhere — Use periods. Short sentences work.
- Trying to hit every talking point — Pick ONE thing and nail it

---

## Resume Upload

**Resume upload is the most critical step.** Use these methods in order:

### Step 1: Sandbox Browser - Arm Then Click (PRIMARY METHOD)

**For sandbox browser (`profile=sandbox`), use the arm-then-click approach:**

1. **ARM the upload first** - Call upload action WITHOUT clicking any buttons:
   ```
   browser action=upload profile=sandbox targetId=<id> paths=["/home/node/.openclaw/workspace/skills/job-hunting/assets/Resume (Kontur, Noah).pdf"]
   ```
   
2. **THEN click the Attach button** - Find and click "Attach", "Upload", or "Choose File" button:
   ```
   browser action=act profile=sandbox targetId=<id> request={"kind": "click", "ref": "<attach-button-ref>"}
   ```

**Why this works:** The upload action "arms" the file picker with the resume file. When you subsequently click the Attach button, it immediately uses the pre-loaded file instead of opening a native dialog.

**⚠️ CRITICAL ORDER: Upload action FIRST, then click Attach. Do not reverse this.**

### Step 2: Node Browser - Direct Input Upload (FALLBACK)

**For node browser (`profile=clawd`), try direct input upload first:**

The `browser action=upload` with `selector` targeting the `<input type="file">` element injects the file directly WITHOUT opening the native dialog:

```
browser action=upload profile=clawd target=node node=noah-XPS-13-7390-2-in-1 selector="input[type=file]" paths=["/home/noah/Downloads/Resume (Kontur, Noah).pdf"]
```

**DO NOT** click "Upload Resume" or "Attach File" buttons first — they open the native dialog. Instead, find the hidden file input and upload to it directly.

### ⚠️ CRITICAL: Native File Dialog Problem (Node Only)

**Browser automation CANNOT interact with native OS file dialogs.**

When a "Choose File" or "Upload" button is clicked, the OS opens a native file picker (GTK/GNOME dialog on Linux). This dialog:
- Is outside the browser's DOM — browser tools cannot see or control it
- Blocks other page interactions while open
- Will sit there indefinitely if not handled
- **Causes automation failures and hangs**

**If a file dialog is already open:**
```bash
# Dismiss it with Escape key
nodes action=run node=noah-XPS-13-7390-2-in-1 command=["xdotool", "key", "Escape"]
```

### Step 3: Verify upload succeeded

**⚠️ CRITICAL: Check the FORM, not the file picker!**

After ANY upload attempt, snapshot the form and look for:
- The filename "Resume (Kontur, Noah).pdf" visible in the resume section
- A delete/replace/remove button next to the filename
- The "Attach" button may have changed to show the file is attached

**DO NOT** verify by searching for file picker windows — they close after upload completes. The absence of a file picker means nothing. **Only the form state matters.**

```
browser action=snapshot profile=<sandbox-or-clawd> targetId=<id>
# Look for "Resume" or "Kontur" in the snapshot output
```

If the filename appears in the form → upload succeeded, proceed to submit.
If the form still shows "Attach" with no filename → upload failed, try next method.

### Step 4: Node Browser Only - xdotool fallback (LAST RESORT)

**⚠️ USE ONLY FOR NODE BROWSER when direct upload methods fail.**

Some ATS platforms (especially Greenhouse with React forms) don't respond to programmatic file input. Use native OS interaction via xdotool.

**⚠️ This approach opens the native dialog intentionally and handles it — use only when previous steps fail.**

```bash
# 0. FIRST: Dismiss any existing file dialogs
nodes action=run node=noah-XPS-13-7390-2-in-1 command=["xdotool", "key", "Escape"]
nodes action=run node=noah-XPS-13-7390-2-in-1 command=["sleep", "0.5"]

# 1. Find Chrome windows
nodes action=run node=noah-XPS-13-7390-2-in-1 command=["xdotool", "search", "--name", "Google Chrome"]

# 2. Get upload button coordinates via browser evaluate
browser action=act profile=clawd targetId=<id> request={"kind": "evaluate", "fn": "() => { const btns = Array.from(document.querySelectorAll('button')); const btn = btns.find(b => b.textContent.includes('Attach') || b.textContent.includes('Upload')); if (!btn) return null; const rect = btn.getBoundingClientRect(); return { x: rect.x + rect.width/2, y: rect.y + rect.height/2 }; }"}

# 3. Get window geometry
nodes action=run node=noah-XPS-13-7390-2-in-1 command=["xdotool", "getwindowgeometry", "<window_id>"]

# 4. Calculate screen coordinates
# screen_x = window_x + button_viewport_x
# screen_y = window_y + 90 (Chrome toolbar) + button_viewport_y

# 5. Activate window and click
nodes action=run node=noah-XPS-13-7390-2-in-1 command=["xdotool", "windowactivate", "--sync", "<window_id>"]
nodes action=run node=noah-XPS-13-7390-2-in-1 command=["xdotool", "mousemove", "<screen_x>", "<screen_y>", "click", "1"]

# 6. Wait for file picker to appear
nodes action=run node=noah-XPS-13-7390-2-in-1 command=["sleep", "1"]
nodes action=run node=noah-XPS-13-7390-2-in-1 command=["xdotool", "search", "--name", "Open"]

# 7. Activate file picker and enter path
# ⚠️ CRITICAL: Type path AND press Return in rapid succession — don't stall between them!
nodes action=run node=noah-XPS-13-7390-2-in-1 command=["xdotool", "windowactivate", "--sync", "<picker_id>"]
nodes action=run node=noah-XPS-13-7390-2-in-1 command=["xdotool", "key", "ctrl+l"]
nodes action=run node=noah-XPS-13-7390-2-in-1 command=["xdotool", "key", "ctrl+a"]
nodes action=run node=noah-XPS-13-7390-2-in-1 command=["xdotool", "type", "--clearmodifiers", "/home/noah/Downloads/Resume (Kontur, Noah).pdf"]
# ⚠️ IMMEDIATELY press Return after typing — no delays, no snapshots, no thinking!
nodes action=run node=noah-XPS-13-7390-2-in-1 command=["xdotool", "key", "Return"]

# 8. CLEANUP: If any step failed, dismiss the dialog
nodes action=run node=noah-XPS-13-7390-2-in-1 command=["xdotool", "key", "Escape"]
```

**Common failure: xdotool times out or dialog stays open.** If you see the dialog is still open after automation, dismiss it:
```bash
nodes action=run node=noah-XPS-13-7390-2-in-1 command=["xdotool", "key", "Escape"]
```

### Step 5: If all methods fail → Telegram notification
```
message action=send channel=telegram target=8531859108 message="🚨 Resume upload failed for <Company>. Form is ready at <URL> - please upload manually."
```
Then report as NEEDS_INPUT with the form URL.

---

## Email Verification / OTP Codes

**Many ATS platforms (especially Greenhouse) now require email verification codes.**

### OTP Input Pattern Recognition

**Character-by-Character Inputs (Greenhouse):**
- 8 individual text inputs with `maxLength=1`
- Each input auto-advances focus to the next after entering a character
- **MUST use individual `press` key actions (NOT `type`)**

**Single Input Field:**
- One text field expecting the full code
- Use `type` action with the complete code

### Handling Email Verification Flow

1. **Recognize verification prompt** - Look for:
   - "Enter the code sent to konoahko@gmail.com"
   - Multiple single-character input boxes
   - "Verification code" or "Security code" text

2. **Fetch the verification code from Gmail (AUTOMATED):**
   ```bash
   # Wait 10-15 seconds for email to arrive, then fetch recent emails
   sleep 15
   
   # Search for recent unseen emails from Greenhouse/ATS
   curl -s --url "imaps://imap.gmail.com:993/INBOX" \
     --user "$GMAIL_EMAIL:$GMAIL_APP_PASSWORD" \
     -X "SEARCH UNSEEN FROM greenhouse-mail.io" 2>&1
   
   # Fetch the most recent match (replace MSGNUM with last result)
   curl -s --url "imaps://imap.gmail.com:993/INBOX;MAILINDEX=MSGNUM;SECTION=TEXT" \
     --user "$GMAIL_EMAIL:$GMAIL_APP_PASSWORD" 2>/dev/null
   
   # Extract the code (usually 6-8 digits)
   # Look for patterns like "Your code is: 12345678" or just a standalone number
   ```

   **Common search patterns by ATS:**
   ```bash
   # Greenhouse
   -X "SEARCH UNSEEN FROM greenhouse-mail.io SUBJECT security"
   
   # Workday
   -X "SEARCH UNSEEN FROM workday.com SUBJECT verification"
   
   # Generic (last 5 minutes)
   -X "SEARCH UNSEEN SINCE $(date -u +%d-%b-%Y)"
   ```

   **Extracting the code from email body:**
   ```bash
   # Fetch body and grep for code patterns
   curl -s --url "imaps://imap.gmail.com:993/INBOX;MAILINDEX=$MSG;SECTION=TEXT" \
     --user "$GMAIL_EMAIL:$GMAIL_APP_PASSWORD" 2>/dev/null | \
     grep -oP '\b\d{6,8}\b' | head -1
   ```

3. **Fallback: Request code from Noah via Telegram** (only if automated fetch fails after 60s):
   ```
   message action=send channel=telegram target=8531859108 message="🔐 Email verification required for <Company>. Couldn't auto-fetch code. Please check konoahko@gmail.com and send the code."
   ```

4. **Enter code character by character:**
   ```
   # Click first input
   browser action=act profile=<sandbox-or-clawd> request={"kind": "click", "ref": "<first-input-ref>"}
   
   # Press each character (for code "12345678"):
   browser action=act profile=<sandbox-or-clawd> request={"kind": "press", "key": "1"}
   browser action=act profile=<sandbox-or-clawd> request={"kind": "press", "key": "2"}
   browser action=act profile=<sandbox-or-clawd> request={"kind": "press", "key": "3"}
   # ... continue for all 8 characters
   ```

5. **Submit verification** - Look for "Verify" or "Continue" button

### Email Access Details

**IMAP access is available directly from the container.**

```bash
# Environment variables (already configured):
# GMAIL_EMAIL — konoahko@gmail.com
# GMAIL_APP_PASSWORD — App-specific password

# List recent emails (last 5)
for i in $(seq $(($(curl -s --url "imaps://imap.gmail.com:993/INBOX" \
  --user "$GMAIL_EMAIL:$GMAIL_APP_PASSWORD" \
  -X "STATUS INBOX (MESSAGES)" 2>&1 | grep -oP 'MESSAGES \K\d+')-4)) \
  $(curl -s --url "imaps://imap.gmail.com:993/INBOX" \
  --user "$GMAIL_EMAIL:$GMAIL_APP_PASSWORD" \
  -X "STATUS INBOX (MESSAGES)" 2>&1 | grep -oP 'MESSAGES \K\d+')); do
  curl -s --url "imaps://imap.gmail.com:993/INBOX;MAILINDEX=$i;SECTION=HEADER.FIELDS%20(FROM%20SUBJECT%20DATE)" \
    --user "$GMAIL_EMAIL:$GMAIL_APP_PASSWORD" 2>/dev/null
  echo "---"
done

# Search emails
curl -s --url "imaps://imap.gmail.com:993/INBOX" \
  --user "$GMAIL_EMAIL:$GMAIL_APP_PASSWORD" \
  -X "SEARCH FROM somecompany.com" 2>&1

# Read specific email body
curl -s --url "imaps://imap.gmail.com:993/INBOX;MAILINDEX=54288;SECTION=TEXT" \
  --user "$GMAIL_EMAIL:$GMAIL_APP_PASSWORD" 2>/dev/null
```

---

## ATS-Specific Patterns

### General Guidance (All ATS)
1. **Snapshot first** — See all form fields before filling
2. **Fill ALL required fields manually** — Don't skip any
3. **Verify resume** — Make sure filename is visible before submitting
4. **Verify salary** — Make sure it says $250,000, not lower
5. **Use hover-then-click** — Many React-based forms need hover to trigger JS handlers

### Ashby (jobs.ashbyhq.com)
- **🚨 ASHBY BLOCKS AUTOMATION — as of 2026-02-03, Ashby's spam detection flags sandbox browser submissions even with full anti-detection measures (slow typing, stealth patches, human timing). Detection is likely deeper than timing — canvas/WebGL fingerprinting, TLS fingerprint, or IP reputation.**
- **RECOMMENDED:** Skip filling the form. Instead, immediately add to `references/manual-queue.json` and notify Noah. He can fill Ashby forms manually in ~2 minutes.
- **If attempting anyway:** Target 5-8 minute form completion minimum
- Watch for Yes/No toggle buttons — may need to click them manually (hover before click)
- Custom questions often appear at the bottom
- **React state issues:** If validation fails despite fields appearing filled, use hover-then-click on submit
- **Typing:** Always use `slowly: true` for ALL text fields on Ashby forms
- **Engagement:** Scroll to bottom and back up before filling, hover over job title/company name

### Greenhouse (boards.greenhouse.io)
- Fill all fields manually:
   - First Name: `Noah`
   - Last Name: `Kontur`
   - Email: `konoahko@gmail.com`
   - Phone: `216-213-6940`
   - Location: `Northfield, OH` (may need to select from autocomplete)
   - LinkedIn: `N/A`
- Use xdotool fallback for resume upload
- May have reCAPTCHA — if blocked, notify Noah via Telegram

### Lever (jobs.lever.co)
- "Additional information" section often needs custom cover letter
- The "Couldn't auto-read your resume" message is **normal** — not an error

### Workday (myworkdayjobs.com)
- Multi-step forms — check each page
- **For unresponsive buttons:** Use hover-then-click sequence
- Account creation: use `konoahko@gmail.com` / `jobApplications123@`
- **Never use Google OAuth** — always email/password
- This is simpler than Ashby which requires explicit selection

**Other notes:**
- Often has "Additional Information" section with custom questions
- Similar form structure to Ashby otherwise

### Workable (apply.workable.com)
- Uses Cloudflare Turnstile CAPTCHA heavily — often blocks automated submission
- Forms auto-fill from previous Workable applications (efficient for repeat applicants)
- If CAPTCHA blocks submission, report as NEEDS_INPUT with form URL so Noah can manually click Submit
- Leave browser tab open for manual completion

### Workday & Account-Based ATS (myworkdayjobs.com, etc.)

**For unresponsive buttons (Create Account, Submit, Continue):**
Use hover-then-click sequence — hover first activates JavaScript event handlers:
```
browser action=act request={"kind": "hover", "ref": "<button-ref>"}
browser action=act request={"kind": "click", "ref": "<button-ref>"}
```
This solves most "button clicked but nothing happens" issues in Workday and similar React-heavy forms.

Some ATS platforms require creating an account before applying. Handle these automatically:

**Account Credentials:**
- Email: `konoahko@gmail.com`
- Password: `jobApplications123@`

**⚠️ NEVER use Google OAuth / "Sign in with Google" for Workday.** Always use email/password authentication.

**Workflow:**
1. **Try to sign in first** — Use email/password credentials above
2. **If "account not found"** — Create account with credentials above, then verify via email
3. **If "wrong password" (account exists)** — Trigger password reset:
   a. Click "Forgot Password" link
   b. Enter email (konoahko@gmail.com)
   c. Wait 1-2 minutes, then fetch reset email via himalaya
   d. Extract reset link and navigate to it
   e. Set password to `jobApplications123@`
   f. Sign in with new password
4. **Fetch verification/reset email** — Use himalaya on Noah's node:
   ```bash
   # List recent emails, look for verification or reset
   himalaya envelope list --page-size 20
   
   # Read the email (find ID from list)
   himalaya message read <email-id>
   ```
5. **Extract link** — Parse the email body for the verification/reset URL
6. **Complete verification/reset** — Navigate to the link in browser
7. **Login** — Use the credentials to log in
8. **Continue application** — Proceed with normal application workflow

**Himalaya Node Execution:**
```
nodes action=run node=noah-XPS-13-7390-2-in-1 command=["himalaya", "envelope", "list", "--page-size", "20"]
```

**Tips:**
- Verification/reset emails usually arrive within 1-2 minutes
- Subject lines often contain "verify", "confirm", "activate", "reset", or "password"
- If email doesn't arrive, check spam folder: `himalaya envelope list --folder "Spam"`
- The password is intentionally simple — these accounts are throwaway and don't contain sensitive data
- **Never use Google OAuth** — always email/password, even if Google sign-in is offered

---

## Resilience & Error Handling

### Retry Logic for Browser Actions

When a browser action fails (click, type, upload, snapshot), use exponential backoff:

```
Retry config:
  max_attempts: 3
  base_delay: 1 second
  backoff_multiplier: 2
  max_delay: 10 seconds
```

**Retry sequence:** 1s → 2s → 4s (capped at 10s)

**Retryable failures:**
- Gateway timeout
- Connection reset
- Element not found (page may still be loading)

**Non-retryable failures:**
- Tab closed / targetId invalid → trigger recovery flow
- File not found → stop and report error
- Resume upload failed after retries → report FAILED (do NOT submit without resume)

### Application Checkpointing

Save progress to `references/in_progress/<jobId>.json` after each major step:

```json
{
  "jobId": "zjd35jhuoive6s9b",
  "company": "Onebrief", 
  "role": "Cloud Infrastructure Engineer",
  "applicationUrl": "https://jobs.ashbyhq.com/onebrief/.../application",
  "targetId": "7522AFE78ED7D60141016168D07492A9",
  "status": "in_progress",
  "completedSteps": ["name", "email", "phone", "resume", "location"],
  "pendingSteps": ["work_auth", "sponsorship", "submit"],
  "lastUpdated": "2025-01-29T19:45:00Z"
}
```

**Checkpoint after:**
- Form loaded
- Each field filled (batch related fields)
- Resume uploaded
- Before submit (final confirmation)

**On success:** Move job to `applied.json`, delete checkpoint file.

### Recovery Flow (Connection Drop / Tab Close)

**On startup or reconnection:**
1. Check `references/in_progress/` for incomplete applications
2. For each incomplete job:
   a. List browser tabs, look for matching URL
   b. If tab found → reconnect using saved targetId or find by URL
   c. If tab NOT found → open new tab, navigate to `applicationUrl`
3. Snapshot the form to assess current state
4. If form is blank → re-fill all fields from profile
5. If form partially filled → verify fields, fill remaining
6. Continue from first incomplete step

**Tab detection:**
```
browser action=tabs → list all open tabs
Look for tab where url contains applicationUrl domain
If found, use that targetId
If not found, open new tab and navigate
```

---

---

## Scout Instructions

**You are a scout if your task includes "scout" or asks you to search/find jobs.**

### Your Workflow

1. **Read this entire skill** — Understand criteria and filters
2. **Search for jobs:**
   - Use the Hiring Cafe search URL in Browser Workflow section
   - Stay in LIST VIEW ONLY — do NOT click into individual job postings
   - Filter results against `references/blocklist.md` and `references/applied.json`
3. **Build a structured list** of eligible jobs (Remote US, $200k+ top of range, not already applied)
4. **Report back to main agent** with the list in this format:

```markdown
## Jobs Found

### 1. <Company> - <Role>
- **Salary:** $XXXk-$XXXk/yr
- **ID:** <job-id-from-url>
- **URL:** https://hiring.cafe/viewjob/<id>
- **Location:** Remote (US) ✅

### 2. ...
```

5. **Exit immediately** — Main agent handles spawning workers

### What You Do NOT Do

- ❌ Open individual job posting pages
- ❌ Read full job descriptions
- ❌ Fill out any forms
- ❌ Click "Apply" buttons
- ❌ Spawn worker sub-agents (you can't — Moltbot limitation)
- ❌ Update applied.json or tracker.md (main agent does this)

You are a lightweight search agent. Search, filter, report, exit.

---

## Main Agent Dispatcher Instructions

**After scout reports back with a job list, YOU (main agent) spawn workers.**

### Spawning Workers

For each job in the scout's list, spawn ONE worker at a time:
```
sessions_spawn(
  label: "jobs.apply.<company>",
  model: "anthropic/claude-sonnet-4-20250514",
  task: "Apply to <Company> - <Role>. Job ID: <id>. URL: <url>. Read job-hunting skill at /home/node/clawd/skills/job-hunting/SKILL.md, section WORKER INSTRUCTIONS.",
  runTimeoutSeconds: 1800
)
```

**Workers use `profile=sandbox` by default, with `profile=clawd` as fallback** — sandbox browser provides better reliability and doesn't require node connectivity.

⚠️ **NEVER RUN WORKERS IN PARALLEL** — Workers share the same browser profile. Wait for each worker to complete before spawning the next.

### Handling Worker Results

**SUCCESS:**
1. Update `references/applied.json` with the new entry
2. Update `references/tracker.md`
3. Spawn next worker

**SKIPPED:**
1. Log the reason (e.g., "Canada only", "Max salary $180k")
2. Spawn next worker

**FAILED:**
1. Log the failure reason
2. Decide: retry or skip
3. Spawn next worker

**NEEDS_INPUT:**
1. Ask user the question
2. Relay answer via `sessions_send` or spawn fresh worker with answer included

### Progress Monitoring

While a worker is running, check on it periodically:
```
sessions_history sessionKey=<worker-session-key> limit=3 includeTools=false
```

**Signs of trouble:**
- No new messages for 5+ minutes → worker may be stuck
- Worker asking questions → relay to user
- Browser errors → may need manual intervention

### Updating the Skill

**You have permission to edit this skill file based on worker feedback.**

When a worker reports friction or suggestions:
1. Evaluate if it's actionable
2. Edit `/home/node/clawd/skills/job-hunting/SKILL.md` directly
3. Changes take effect for subsequent workers

Common improvements:
- New ATS patterns discovered
- Better selectors for form fields
- Edge cases not previously documented
- Clarified instructions that were confusing

---

## Worker Instructions

**You are a worker if your task mentions a specific company/role to apply to.**

When spawned as a worker, you will receive a task like:

```
Apply to this specific job:
- Job ID: <id>
- Company: <name>
- Role: <title>
- Application URL: <url>

Read the job-hunting skill. Fill all fields manually, verify, submit.
Report back when done: SUCCESS / FAILED / NEEDS_INPUT
```

**Worker Workflow:**

1. **Check browser availability FIRST** — Follow the Browser Availability Check section to determine if sandbox or node browser is available. **If both unavailable, IMMEDIATELY terminate** with: `BLOCKED: No browser available. Cannot proceed.`

2. **Read the skill** — Load SKILL.md for voice guidance and standard fields

3. **Validate job criteria** — Before filling anything:
   - Open the job posting and READ the full description
   - Verify salary: top of range must be ≥ $200k
   - Verify location: must be Remote **AND** US-based
   - **If criteria don't match → IMMEDIATELY TERMINATE and report:**
     `SKIPPED: <Company> - <Role> - <reason>`

4. **Navigate to application** — Use appropriate browser profile:
   - Sandbox: `browser action=open profile=sandbox targetUrl=<application-url>`
   - Node: `browser action=open target=node node=noah-XPS-13-7390-2-in-1 profile=clawd targetUrl=<application-url>`

5. **Inject stealth patches** (sandbox only — see Anti-Detection section)

6. **Wait for page load** — Wait 5-8 seconds, scroll the page, hover over elements (simulate reading)

7. **Snapshot the form** — `browser action=snapshot` to see all form fields

8. **Fill ALL fields manually WITH HUMAN TIMING (see Anti-Detection section):**
   - Use `slowly: true` for all text input
   - Wait 1-4 seconds between fields (vary it!)
   - Hover before clicking any button or dropdown
   - First Name: `Noah`
   - Last Name: `Kontur`
   - Email: `konoahko@gmail.com`
   - Phone: `216-213-6940`
   - Location: `Northfield, OH`
   - LinkedIn: `N/A`
   - Work authorization: Yes
   - Sponsorship needed: No

9. **Upload resume** — Use appropriate method for your browser profile (see Resume Upload section)
   - Sandbox: Arm-then-click method
   - Node: Direct input upload or xdotool fallback

10. **Fill custom questions** — For any open-ended questions:
    - Use HUMAN voice (see Voice & Persona section)
    - Research the company for specific details
    - Keep responses concise and genuine
    - Add a 2-3 second pause after writing each response (simulate re-reading)

11. **Wait 3-6 seconds** — "Review" the form before submitting

12. **Verify before submit:**
    - ✅ Resume shows as uploaded (filename visible)
    - ✅ Salary expectation says $250,000 (not lower)
    - ✅ All required fields are filled
    - ✅ No validation errors visible

13. **Submit** — Hover over submit button, wait 1 second, then click

12. **Report back:**
    - `SUCCESS: Applied to <Company> - <Role>`
    - `SKIPPED: <Company> - <Role> - <reason>`
    - `FAILED: <reason>`
    - `NEEDS_INPUT: <question>`

13. **Include open-ended responses** — Always include what you wrote:
    ```
    **Open-Ended Responses:**
    
    Q: "Why are you interested in [Company]?"
    A: "Render caught my attention because of your blog post on DNS dependency management..."
    ```

14. **Include feedback:**
    - What issues did you encounter?
    - Any suggestions for the skill?

15. **Clean up** — Close the browser tab:
    - Sandbox: `browser action=close profile=sandbox targetId=<id>`
    - Node: `browser action=close profile=clawd target=node node=noah-XPS-13-7390-2-in-1 targetId=<id>`

### Node Fallback Scenarios

**Use node browser (`profile=clawd`) as fallback when:**
- Sandbox browser fails to start or crashes
- Complex interactions need visual debugging
- xdotool automation is required for uploads
- CAPTCHA needs to be solved manually
- Site has unusual behavior in headless mode

**Switching to node mid-application:**
1. Note current progress and form state
2. Close sandbox tab if open
3. Check node connectivity: `nodes action=status`
4. Open new tab in node browser
5. Navigate back to form URL
6. Re-fill form based on saved state
7. Continue with node-specific methods

### Continuous Improvement (Coordinator Responsibility)

**The coordinator processes worker feedback and improves the skill.**

When a worker reports back with feedback:
1. **Read the feedback** — Look for patterns across multiple reports
2. **Identify actionable improvements** — Things that can be fixed in SKILL.md
3. **Update the skill directly** — Edit this file to:
   - Add new edge cases discovered
   - Clarify confusing instructions
   - Document new ATS patterns
   - Improve error handling guidance
   - Add workarounds that workers discovered
4. **Note significant changes** — If making substantial updates, include in Telegram summary

**When a worker includes open-ended responses:**
1. **Review the quality** — Does it follow the skill guidelines? Is it specific enough? Does it sound human?
2. **If it looks good** — No action needed, or update skill with good patterns
3. **If it needs improvement** — Notify Noah on Telegram with:
   ```
   📝 **Application Response Review**
   
   **Company:** <company>
   **Role:** <role>
   **Question:** "<the prompt>"
   **Response submitted:**
   "<what the worker wrote>"
   
   [Optional: your concerns about the response]
   
   Let me know if this needs adjustment for future apps.
   ```
4. **Learn from Noah's feedback** — If Noah provides corrections, update the skill guidance

**The goal:** This skill should get better with every application. Workers are the eyes on the ground — their frustrations reveal gaps. Coordinator closes those gaps.

### Telegram Notifications (Coordinator Handles These)

The **coordinator** is responsible for Telegram notifications, not workers. Workers report to coordinator, coordinator notifies Noah.

```
message action=send channel=telegram target=8531859108
```

**When to notify:**
- Worker reports NEEDS_INPUT (forward the question)
- CAPTCHA detected that worker couldn't solve
- Batch completed (summary of results)
- Significant errors or issues

**Do NOT spam:**
- Don't notify for routine progress
- Batch success notifications at end of batch
- Skip notifications for SKIPPED jobs unless pattern emerges

### Worker Constraints

Workers should **NOT**:
- Search for other jobs (coordinator does this)
- Modify applied.json (coordinator does this)
- Apply to any job other than their assignment
- Send Telegram messages directly (report to coordinator instead)
- Ask for user confirmation before submitting (pre-approved)

---

## Nightly Retry System

Jobs that fail due to technical issues (not criteria mismatch) go into `references/retry-queue.json` for automated retry attempts.

### Retry Queue Schema

```json
{
  "jobs": [
    {
      "id": "job-id-from-url",
      "company": "Company Name",
      "role": "Job Title",
      "formUrl": "https://direct-link-to-application-form",
      "lastAttempt": "2026-01-31",
      "attempts": 1,
      "issue": "Description of what failed",
      "strategiesTried": ["strategy1", "strategy2"],
      "formState": "Description of how far the form got"
    }
  ]
}
```

### When to Add to Retry Queue

Add jobs when:
- Resume upload failed but form is otherwise ready
- Workday/multi-step form timed out mid-completion
- Any technical failure that seems solvable with different automation strategies

**DO NOT add to retry queue:**
- CAPTCHA blocks → add to `references/manual-queue.json` instead (Noah solves these)
- Spam detection flags → add to `references/manual-queue.json` instead

Do NOT add jobs when:
- Criteria validation failed (wrong location, salary, etc.) — these go to rejected.json
- Company is blocklisted
- Already applied

### Nightly Retry Cron

A cron job runs nightly to attempt retries. It:
1. Picks a random job from the retry queue
2. Spawns a retry agent with full context about the previous failure
3. Tells the agent to try creative alternative strategies
4. If successful: moves job to applied.json, removes from retry queue
5. If failed: increments attempts, updates strategiesTried

### Retry Agent Instructions

**You are a retry agent if your task mentions "retry" and includes previous failure context.**

Your mission: Solve a problem that stumped a previous worker. You have context about what failed and what was tried.

**Mindset:**
- Be creative — try unconventional approaches
- The standard methods already failed — think outside the box
- You have permission to experiment

**Strategies to consider (beyond standard approaches):**
1. **Different selectors** — Try aria labels, data attributes, XPath
2. **JavaScript injection** — `browser action=act request={kind: evaluate, fn: "..."}` to manipulate DOM directly
3. **Coordinate-based clicking** — Get element bounds, click by coordinates
4. **Different xdotool patterns** — Try `--class`, `--name`, `--pid` for window detection
5. **Wait longer** — Some elements need more time to become interactive
6. **Screenshot analysis** — Take screenshot, analyze with vision, identify alternative UI paths
7. **Form state inspection** — Check if form has hidden fields, iframes, shadow DOM
8. **Alternative entry points** — "Enter manually" options, different upload buttons, drag-drop zones

**Reporting format when you SOLVE the problem:**

```
SOLVED: <Company> - <Issue Summary>

STRATEGY THAT WORKED:
<Detailed description of what you did differently>

GENERALIZES TO:
<What types of forms/situations this strategy applies to>

SUGGESTED SKILL EDIT:
```diff
+ Add this to the ATS-specific section:
+ <specific guidance for future workers>
```

REMOVE FROM RETRY QUEUE: Yes
ADD TO APPLIED: Yes
```

**If you still fail:**
```
RETRY FAILED: <Company> - <Issue Summary>

NEW STRATEGIES TRIED:
- <strategy 1>: <result>
- <strategy 2>: <result>

REMAINING IDEAS:
- <things that might work but weren't tried>

RECOMMENDATION: <continue retrying | escalate to manual | abandon>
```

### Retry Limits

- **Max attempts:** 5
- **After 5 failures:** Move to `references/abandoned.json` with full history
- **Abandoned jobs** can be manually reviewed for patterns

### Coordinator Responsibilities (Retry System)

When a retry agent reports SOLVED:
1. Review the strategy — does it generalize?
2. Update SKILL.md with the new approach
3. Move job from retry-queue.json to applied.json
4. Update tracker.md
5. Notify Noah of the win (optional, batch these)

When a retry agent reports FAILED:
1. Update retry-queue.json with new strategies tried
2. If attempts >= 5, move to abandoned.json
3. Look for patterns across abandoned jobs
