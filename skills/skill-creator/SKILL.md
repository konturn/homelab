---
name: skill-creator
description: Create or update AgentSkills. Use when designing, structuring, or packaging skills with scripts, references, and assets.
---

# Skill Creator

This skill provides guidance for creating effective skills.

## About Skills

Skills are modular, self-contained packages that extend Codex's capabilities by providing
specialized knowledge, workflows, and tools. Think of them as "onboarding guides" for specific
domains or tasks—they transform Codex from a general-purpose agent into a specialized agent
equipped with procedural knowledge that no model can fully possess.

### What Skills Provide

1. Specialized workflows - Multi-step procedures for specific domains
2. Tool integrations - Instructions for working with specific file formats or APIs
3. Domain expertise - Company-specific knowledge, schemas, business logic
4. Bundled resources - Scripts, references, and assets for complex and repetitive tasks

## Core Principles

### Concise is Key

The context window is a public good. Skills share the context window with everything else: system prompt, conversation history, other Skills' metadata, and the actual user request.

**Default assumption: The agent is already very smart.** Only add context it doesn't already have. Challenge each piece: "Does the agent really need this?" and "Does this paragraph justify its token cost?"

Prefer concise examples over verbose explanations.

### Set Appropriate Degrees of Freedom

Match specificity to the task's fragility and variability:

**High freedom (text-based instructions)**: Multiple approaches valid, decisions depend on context.

**Medium freedom (pseudocode or parameterized scripts)**: Preferred pattern exists, some variation acceptable.

**Low freedom (specific scripts, few parameters)**: Operations fragile, consistency critical, specific sequence required.

### Anatomy of a Skill

```
skill-name/
├── SKILL.md (required)
│   ├── YAML frontmatter (name, description)
│   └── Markdown instructions
├── references/       - Documentation loaded as needed
├── scripts/          - Executable code
├── assets/           - Files used in output
└── feedback.jsonl    - Sub-agent feedback (if skill spawns sub-agents)
```

#### SKILL.md (required)

- **Frontmatter** (YAML): `name` and `description` fields. Description is the primary trigger — be clear about what the skill does and when to use it.
- **Body** (Markdown): Instructions loaded AFTER the skill triggers.

#### Bundled Resources (optional)

**Scripts (`scripts/`)**: Executable code for deterministic tasks or frequently rewritten operations.

**References (`references/`)**: Documentation loaded as needed. Keep SKILL.md lean.

**Assets (`assets/`)**: Files used in output (templates, images, fonts) — not loaded into context.

### Progressive Disclosure

Three-level loading system:
1. **Metadata** - Always in context (~100 words)
2. **SKILL.md body** - When skill triggers (<5k words)
3. **Bundled resources** - As needed

Keep SKILL.md under 500 lines. Split into reference files when approaching this limit.

---

## Sub-Agent Feedback Pattern

**For skills that spawn sub-agents**, implement a feedback loop so the skill improves from real usage.

### Why Feedback Matters

Sub-agents encounter real-world friction: unclear instructions, missing API details, edge cases, pipeline failures. This information should flow back to improve the skill.

### Implementation

**1. Create feedback file:**
```
skill-name/feedback.jsonl
```

**2. Add feedback section to SKILL.md:**

```markdown
## Sub-Agent Feedback — REQUIRED

Before exiting, report feedback to improve this skill.

Ask yourself:
- Did I hit friction or confusion?
- Was something unclear or missing?
- Did I have to improvise?
- What would have made this easier?

**Append to feedback.jsonl:**
\`\`\`bash
echo '{"ts":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'","session":"<label>","outcome":"success|partial|failed","friction":"what was hard","suggestion":"how to improve","notes":"context"}' >> /path/to/skill/feedback.jsonl
\`\`\`

**Outcome values:**
- `success` — Completed without issues
- `partial` — Completed but hit friction
- `failed` — Could not complete

Even successful runs should report.
```

**3. Periodic review:**

The main session (or a cron job) reviews feedback.jsonl and updates SKILL.md:
- Clarify confusing sections
- Add missing edge cases
- Document common errors and fixes
- Improve examples based on successful runs

### Feedback Schema

```json
{
  "ts": "ISO timestamp",
  "session": "sub-agent session label",
  "outcome": "success|partial|failed",
  "friction": "what was difficult or unclear",
  "suggestion": "specific improvement to the skill",
  "notes": "additional context"
}
```

### Example Feedback Entries

```json
{"ts":"2026-01-31T02:00:00Z","session":"gitlab.mr.21.feedback","outcome":"success","friction":"none","suggestion":"none","notes":"Clean run"}
{"ts":"2026-01-31T02:30:00Z","session":"gitlab.mr.22.feedback","outcome":"partial","friction":"ansible-lint error not covered","suggestion":"Add common ansible-lint fixes section","notes":"Had to google it"}
{"ts":"2026-01-31T03:00:00Z","session":"gitlab.mr.14.feedback","outcome":"failed","friction":"No local testing instructions","suggestion":"Add docker-compose local test guide","notes":"Gave up after 3 attempts"}
```

---

## Skill Creation Process

1. **Understand** the skill with concrete examples
2. **Plan** reusable contents (scripts, references, assets)
3. **Initialize** the skill (run init_skill.py if available)
4. **Edit** the skill (implement resources, write SKILL.md)
5. **Package** the skill (run package_skill.py if available)
6. **Iterate** based on real usage and feedback

### Skill Naming

- Lowercase letters, digits, hyphens only
- Under 64 characters
- Prefer short, verb-led phrases
- Namespace by tool when helpful (e.g., `gh-address-comments`)

### Step 1: Understanding with Examples

Ask:
- What functionality should the skill support?
- Can you give examples of how it would be used?
- What would a user say that should trigger this skill?

### Step 2: Planning Contents

For each example:
1. How would you execute from scratch?
2. What scripts/references/assets would help with repeated execution?

### Step 3: Initialize

If creating new, use init script if available:
```bash
scripts/init_skill.py <skill-name> --path <output-dir> [--resources scripts,references,assets]
```

### Step 4: Edit

**Frontmatter:**
- `name`: Skill name
- `description`: What it does AND when to use it (this is the trigger)

**Body:**
- Instructions for using the skill
- References to bundled resources
- Sub-agent feedback section (if applicable)

### Step 5: Package

```bash
scripts/package_skill.py <path/to/skill-folder>
```

### Step 6: Iterate

1. Use skill on real tasks
2. Notice struggles or inefficiencies  
3. **Review feedback.jsonl** (if skill spawns sub-agents)
4. Update SKILL.md and resources
5. Test again

---

## What NOT to Include

- README.md, INSTALLATION_GUIDE.md, CHANGELOG.md
- Setup/testing procedures
- User-facing documentation
- Auxiliary context about creation process

The skill should only contain what an AI agent needs to do the job.
