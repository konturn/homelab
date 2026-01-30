---
name: job-hunting
description: Search and apply for remote software engineering jobs. Use when searching job boards, evaluating opportunities, preparing applications, or tracking job search progress. Handles company blocklists, salary requirements, and application customization.
---

# Job Hunting Skill

## Target Criteria

- **Location:** Remote only (US-based). **Never willing to relocate.**
- **Salary:** Top of range ≥ $200k (transparent salaries preferred)
- **Roles:** Infrastructure Engineer, Cloud Engineer, DevOps, SRE, Platform Engineer
- **Commitment:** Full-time

---

## ⚠️ CRITICAL REQUIREMENTS

### Three-Tier Architecture: Main → Coordinator → Workers

```
MAIN AGENT (conversation with user)
    ↓ spawns once, returns immediately (fire-and-forget)
COORDINATOR SUB-AGENT (long-running batch manager)
    ↓ spawns ONE at a time, monitors, waits for completion
WORKER SUB-AGENT (does ONE job application)
    ↓ reports back to coordinator
Coordinator updates tracking, improves skill, spawns next worker
```

**Why this architecture:**
- Main agent is NOT blocked — spawns coordinator and returns to user immediately
- Coordinator handles serial execution (ONE worker at a time — multiple clobber each other on the node)
- Workers get fresh context per application
- Feedback loops work: workers report → coordinator updates skill → future workers benefit
- Coordinator forwards important messages to Telegram

### Main Agent Responsibilities (YOU, talking to user)

When user requests job applications:
1. Spawn a coordinator sub-agent with the job batch or search criteria
2. Return immediately — do NOT wait for coordinator to finish
3. User can check status anytime by asking

**Spawn coordinator like this:**
```
sessions_spawn(
  label: "job-coordinator",
  model: "anthropic/claude-sonnet-4-20250514",
  task: "You are the job application coordinator. Read the job-hunting skill. Search for [N] jobs matching criteria, then apply to each ONE AT A TIME. See COORDINATOR INSTRUCTIONS in the skill.",
  runTimeoutSeconds: 7200  // 2 hours for a batch
)
```

### Coordinator Sub-Agent Responsibilities

**You are the coordinator if your task mentions "coordinator" or "batch".**

1. **Read this skill fully** — Understand all requirements
2. **Search for jobs** — Use Hiring Cafe, filter against blocklist/applied.json
3. **For each job, spawn ONE worker at a time:**
   ```
   sessions_spawn(
     label: "job-worker-<company>",
     model: "anthropic/claude-sonnet-4-20250514",
     task: "Apply to <Company> - <Role>. Job ID: <id>. URL: <url>. Browser profile: job-1. Read job-hunting skill, section WORKER INSTRUCTIONS.",
     runTimeoutSeconds: 1800  // 30 min per application
   )
   ```
4. **Wait for worker completion** — Poll `sessions_history` every 30-60 seconds
5. **Process worker report:**
   - SUCCESS → Update applied.json and tracker.md
   - SKIPPED → Note reason, move to next job
   - FAILED → Log failure, consider retry or skip
   - NEEDS_INPUT → Forward to Telegram, wait for response, relay back
6. **Update skill based on feedback** — If worker reports friction or suggestions, edit SKILL.md
7. **Only after worker completes** → Spawn next worker
8. **Send summary to Telegram** when batch completes

**⚠️ NEVER run multiple workers simultaneously.** Browser profiles clobber each other on the node.

**Progress monitoring:** Every 60 seconds while a worker is running:
- Check `sessions_history` for the worker
- If worker appears stuck (no new messages for 5+ minutes), investigate
- If worker needs input, handle it or forward to Telegram

### Worker Sub-Agent Responsibilities

**You are a worker if your task mentions a specific company/role to apply to.**

See WORKER INSTRUCTIONS section below for full details. Key points:
- Apply to ONE job only
- Use your assigned browser profile (job-1, job-2, etc.)
- Report back: SUCCESS / SKIPPED / FAILED / NEEDS_INPUT
- Include feedback on what worked and what didn't

### Resume Upload is MANDATORY
**An application is NOT successful unless the resume has been uploaded.** If the resume upload fails:
1. Retry with different selectors (file input, button, drag-drop area)
2. Try JavaScript-based file injection
3. If all attempts fail → report as **FAILED**, not SUCCESS
4. Never submit an application without confirming the resume file was attached

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
Send Telegram notification immediately:
```
message action=send channel=telegram target=8531859108 message="🔐 CAPTCHA detected for <Company>. Form is ready at <URL> - please solve it manually."
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
- **Stories:** See `references/stories.md` — personal accomplishment stories for applications
- **Resume:** `assets/Resume (Kontur, Noah).pdf` — upload this for applications
- **Resume path on laptop:** `/home/noah/Downloads/Resume (Kontur, Noah).pdf`
- **LinkedIn:** N/A — leave blank or enter "N/A" (no LinkedIn profile)
- **Telegram notifications:** Chat ID `8531859108` — alert Noah when input needed

## Voice & Persona

**When filling applications, BE Noah.** Write in first person. Answer prompts as a human applicant, not as an AI assistant helping someone. Never say "Noah is..." — say "I am..."

**Name handling:**
- Regular name fields: **Noah Kontur** (no middle name)
- Legal signature / full legal name: **Noah P. Kontur** (middle INITIAL only, not "Patrick")
- Never use full middle name unless explicitly required

**Date verification:**
- Before filling any date fields, run: `TZ=America/New_York date "+%Y-%m-%d"`
- Use this verified date for "applied on" or similar fields
- Do NOT assume the date from context or memory — always verify

Example:
- ❌ "Noah ran a marathon in 2021..."
- ✅ "I ran a marathon in 2021..."

This applies to all free-text fields, cover letters, and any written responses.

**Writing style:**
- **No em dashes (—).** Use commas, periods, or "and" instead.
- Avoid corporate buzzwords: "synergy," "leverage," "align perfectly," "passionate about"
- Write like a human, not a press release
- Short sentences are fine. Don't over-connect everything.
- Specifics over generics. Names, numbers, and concrete details beat vague claims.

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
> I'm drawn to Render's goal of eliminating undifferentiated infrastructure work. At Nvidia, I saw how much engineering time gets lost to cloud complexity. The technical depth here is compelling, what with Kubernetes at scale, custom traffic routing, and container orchestration. I love Render's blog posts on Knative scaling and DNS dependency management. It signals a team that cares about craft.

**Bad example:**
> I'm excited about Render because it represents the future of cloud infrastructure, making deployment effortless so developers can focus on building. Render's approach of abstracting away cloud complexity aligns perfectly with my experience.

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
1. List existing tabs: `browser action=tabs target=node node=noah-XPS-13-7390-2-in-1 profile=clawd`
2. If a Hiring Cafe tab exists, use its `targetId` — do NOT open a new tab
3. If no Hiring Cafe tab exists, open ONE tab and track its `targetId`
4. Use that SAME `targetId` for ALL subsequent actions on that tab

**Search Workflow:**
1. Navigate directly to this EXACT URL (hardcoded, do NOT use saved searches):
   ```
   https://hiring.cafe/?searchState=%7B%22locations%22%3A%5B%7B%22formatted_address%22%3A%22United+States%22%2C%22types%22%3A%5B%22country%22%5D%2C%22geometry%22%3A%7B%22location%22%3A%7B%22lat%22%3A%2241.3284%22%2C%22lon%22%3A%22-81.4981%22%7D%7D%2C%22id%22%3A%22user_country%22%2C%22address_components%22%3A%5B%7B%22long_name%22%3A%22United+States%22%2C%22short_name%22%3A%22US%22%2C%22types%22%3A%5B%22country%22%5D%7D%5D%2C%22options%22%3A%7B%22flexible_regions%22%3A%5B%22anywhere_in_continent%22%2C%22anywhere_in_world%22%5D%7D%2C%22workplace_types%22%3A%5B%22Remote%22%5D%7D%5D%2C%22searchQuery%22%3A%22infrastructure+engineer+cloud+engineer%22%2C%22restrictJobsToTransparentSalaries%22%3Atrue%2C%22maxCompensationLowEnd%22%3A%22200000%22%7D
   ```
2. Wait for page load, then snapshot
3. Results should show: Infrastructure/Cloud roles, $200k+, US, Remote ONLY, transparent salaries
4. Filter results against blocklist and applied.json before presenting to user
5. All results should be Remote — if you see Onsite/Hybrid, something is wrong

## Browser Connection Details

**Main agent (searching/coordinating):**
```
target: node
node: noah-XPS-13-7390-2-in-1
profile: clawd
```

**Sub-agents (applying to jobs):**
Each sub-agent MUST use its assigned browser profile to avoid tab conflicts.

Available profiles for job applications:
- `job-1` — port 18801
- `job-2` — port 18802
- `job-3` — port 18803
- `job-4` — port 18804

**Opening a new tab (main agent):**
```
browser action=open target=node node=noah-XPS-13-7390-2-in-1 profile=clawd targetUrl=<url>
```

**Opening a new tab (sub-agent with assigned profile):**
```
browser action=open profile=<assigned-profile> targetUrl=<url>
```

**Getting tab list:**
```
browser action=tabs target=node node=noah-XPS-13-7390-2-in-1 profile=clawd
```

## ATS-Specific Patterns

### Ashby (jobs.ashbyhq.com)
- **Autofill:** Has "Upload file" button that auto-fills fields from resume
- **Location field:** Combobox that shows suggestions as you type — type location, wait for dropdown, click the matching option
- **Yes/No toggle buttons:** Often styled as toggle buttons, not radio buttons. These can be finicky — use `hover` action on the button before `click` to ensure the selection registers properly.

#### Resume Upload Strategy (All ATS)

**Step 1: Try browser upload action**
```
browser action=upload profile=<profile> targetId=<id> selector="input[type=file]" paths=["/home/noah/Downloads/Resume (Kontur, Noah).pdf"]
```

**Step 2: Verify upload succeeded**
Snapshot the form and check for filename visible (e.g., "Resume (Kontur, Noah).pdf" with a delete/replace button).

**Step 3: If upload failed → Use xdotool fallback**

Some ATS platforms (especially Ashby with react-dropzone) don't respond to programmatic file input. Use native OS interaction via xdotool:

```bash
# 1. Find and activate the Chrome window
xdotool search --name "Google Chrome"  # returns window ID
xdotool windowactivate --sync <window_id>

# 2. Get upload button coordinates via browser tool
browser action=act request={"kind": "evaluate", "fn": "() => { const btns = Array.from(document.querySelectorAll('button')); const btn = btns.find(b => b.textContent.includes('Upload File') || b.textContent.includes('Replace')); if (!btn) return null; const rect = btn.getBoundingClientRect(); return { x: rect.x + rect.width/2, y: rect.y + rect.height/2 }; }"}

# 3. Get window geometry to calculate screen coordinates
xdotool getwindowgeometry <window_id>  # returns position and size

# 4. Calculate screen coords: window_x + viewport_x, window_y + ~90 (chrome) + viewport_y
# Click the upload button
xdotool mousemove <screen_x> <screen_y> click 1

# 5. Wait for file picker, then find and activate it
xdotool search --name "Open"  # GNOME file picker
xdotool windowactivate --sync <picker_window_id>

# 6. Use Ctrl+L to open path entry, type path, press Enter
xdotool key ctrl+l
xdotool key ctrl+a
xdotool type --clearmodifiers "/home/noah/Downloads/Resume (Kontur, Noah).pdf"
xdotool key Return
```

**xdotool is available on Noah's node:**
```
nodes action=run node=noah-XPS-13-7390-2-in-1 command=["xdotool", "..."]
```

**Step 4: If xdotool also fails → Telegram notification**
```
message action=send channel=telegram target=8531859108 message="🚨 Resume upload failed for <Company>. Form is ready at <URL> - please upload manually."
```

**Always verify:** After any upload method, snapshot the form and confirm the filename is visible before submitting.

### Greenhouse (boards.greenhouse.io)
- Different structure, typically more fields
- May require cover letter (check if optional)
- **Phone country code:** Often has a separate "Country" dropdown specifically for phone number area code, distinct from the "In which country do you reside?" question. Easy to miss — look for it near the phone number field.

### Lever (jobs.lever.co)

**Resume Upload:**
- Lever's file input often doesn't respond to programmatic upload
- **Preferred method:** Use xdotool to click the ATTACH/UPLOAD button, then use native file picker
- The "Couldn't auto-read your resume" message is **normal and expected** — not an error. Lever just means it didn't parse fields from the PDF. Continue with the application.

**Demographic Survey:**
- Lever's demographic questions (gender, race, veteran status, disability) are truly optional
- You can leave them blank without selecting "Prefer not to disclose"
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

## Coordinator Instructions

**You are the coordinator if your task includes "coordinator" or asks you to manage a batch of applications.**

### Your Workflow

1. **Read this entire skill** — Understand criteria, voice, ATS patterns
2. **Search for jobs:**
   - Use the Hiring Cafe search URL in Browser Workflow section
   - Filter results against `references/blocklist.md` and `references/applied.json`
   - Build a list of eligible jobs (Remote, $200k+ top of range, not already applied)
3. **For each job:**
   a. Spawn ONE worker sub-agent (see spawn example below)
   b. Poll worker status every 30-60 seconds via `sessions_history`
   c. Wait for worker to complete (SUCCESS/SKIPPED/FAILED/NEEDS_INPUT)
   d. Handle the result (see below)
   e. Only then spawn the next worker
4. **When batch completes**, send summary to Telegram

### Spawning a Worker

```
sessions_spawn(
  label: "job-worker-<company>",
  model: "anthropic/claude-sonnet-4-20250514",
  task: "Apply to <Company> - <Role>. Job ID: <id>. Hiring Cafe URL: <url>. Browser profile: job-1. Read the job-hunting skill at /home/node/clawd/skills/job-hunting/SKILL.md, section WORKER INSTRUCTIONS.",
  runTimeoutSeconds: 1800
)
```

**Always use browser profile `job-1`** — since only one worker runs at a time, they all share the same profile.

### Handling Worker Results

**SUCCESS:**
1. Update `references/applied.json` with the new entry
2. Update `references/tracker.md` 
3. Review any open-ended responses — if quality is poor, note for skill improvement
4. Process feedback — if worker suggests improvements, edit this skill file

**SKIPPED:**
1. Log the reason (e.g., "YOE too high", "Not actually remote")
2. Move to next job
3. If many jobs are being skipped for the same reason, adjust search criteria

**FAILED:**
1. Log the failure reason
2. Decide: retry (if transient error) or skip (if fundamental issue)
3. If resume upload failed, consider flagging for manual completion

**NEEDS_INPUT:**
1. Forward the question to Telegram:
   ```
   message action=send channel=telegram target=8531859108 message="🚨 Job App Needs Input\n\nCompany: <company>\nRole: <role>\nQuestion: <question>\n\nReply here to continue."
   ```
2. Wait for response (check periodically or wait for session message)
3. Relay answer back to worker via `sessions_send`

### Progress Monitoring

While a worker is running, check on it every 60 seconds:
```
sessions_history sessionKey=<worker-session-key> limit=3 includeTools=false
```

**Signs of trouble:**
- No new messages for 5+ minutes → worker may be stuck
- Worker asking questions but not receiving answers → relay to Telegram
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
- Browser Profile: job-1  ← YOUR ISOLATED BROWSER

Read the job-hunting skill. Follow the application workflow.
Use ONLY your assigned browser profile (job-1/job-2/job-3/job-4).
Report back when done: SUCCESS / FAILED / NEEDS_INPUT
```

**Sub-agent workflow:**

1. **Read the skill** — Load SKILL.md for profile, resume path, persona guidance
2. **Validate job criteria FIRST** — Before filling anything:
   - Open the job posting and READ the full description
   - Verify salary: top of range must be ≥ $200k
   - Verify location: must be Remote (not Hybrid/Onsite only)
   - **If criteria don't match → IMMEDIATELY TERMINATE and report:**
     `SKIPPED: <Company> - <Role> - <reason: e.g. "Hybrid only" or "Max salary $180k">`
   - Do NOT waste time on applications that don't meet criteria
3. **Create checkpoint** — `in_progress/<jobId>.json` for crash recovery
4. **Open new tab in YOUR profile** — `browser action=open profile=<assigned-profile> targetUrl=<url>`
   - CRITICAL: Always use the profile assigned in your task
   - Do NOT use `profile=clawd` or `target=node` — those are for the main agent
   - Each sub-agent gets its own isolated browser instance
4. **Fill application** — Follow standard workflow, BE Noah (first person)
5. **Handle prompts:**
   - Standard fields → fill from profile
   - Stories → use stories.md, or NEEDS_INPUT if none fit
   - "Why this company?" → research and craft response per skill guidelines
6. **Submit** — Click submit (no user confirmation needed for sub-agents)
7. **Verify resume upload** — Before submitting, CONFIRM the resume was attached:
   - Check for filename visible in the form
   - Look for upload success indicator
   - If resume upload failed → DO NOT SUBMIT → report as FAILED
8. **Report back** — Send result to main session:
   - `SUCCESS: Applied to <Company> - <Role>` (ONLY if resume was uploaded)
   - `SKIPPED: <Company> - <Role> - <reason>` (job didn't meet criteria after reading description)
   - `FAILED: <reason>` (including "resume upload failed")
   - `NEEDS_INPUT: <question that needs Noah's answer>`
9. **Include open-ended responses** — If you wrote ANY free-text responses (e.g., "Why are you interested?", cover letter, personal story), include them in your report:
   ```
   **Open-Ended Responses:**
   
   Q: "Why are you interested in [Company]?"
   A: "I'm drawn to Render's goal of eliminating undifferentiated infrastructure work. At Nvidia, I saw how much engineering time gets lost to cloud complexity..."
   ```
   This lets the main agent review quality and provide feedback to Noah if needed.

10. **Include feedback** — With EVERY report (success or failure), add a `**Feedback:**` section:
   - What worked well?
   - What was frustrating or harder than expected?
   - What's missing from the skill that would have helped?
   - Any edge cases the skill doesn't cover?
   - Suggestions for improvement?
   
   Example:
   ```
   SUCCESS: Applied to Render - Software Engineer, Infrastructure
   
   **Open-Ended Responses:**
   Q: "Why are you interested in Render?"
   A: "I'm drawn to Render's goal of eliminating undifferentiated infrastructure work..."
   
   **Feedback:**
   - xdotool fallback worked great for resume upload
   - The "Why this company?" guidance was helpful
   - Frustration: Had to guess at button coordinates, wish there was a more reliable way
   - Suggestion: Add guidance for handling multi-page application forms
   ```
11. **Clean up:**
   - Delete checkpoint file if created
   - **Close the browser tab on SUCCESS:** `browser action=close profile=<your-profile> targetId=<your-targetId>`
   - This keeps browser clean for next sub-agent

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
