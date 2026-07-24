# Three-auditor panel

When auditing or validating a list of checks, use this concurrency rule:

1. **One shared prompt per check** (same scope, intent, deliberate decisions, non-goals).
2. **Three seats, concurrent:** [buggie](../.claude/skills/buggie/SKILL.md) · [no-mistakes](../.claude/skills/no-mistakes/SKILL.md) · [ponytail ultra](../.claude/skills/ponytail/SKILL.md) (or ponytail-review).
3. **Wait for all three**, synthesize, then start the **next** check.
4. **Never** launch (N checks × 3) in one blast — only **3 at a time**.

Full protocol, prompt template, and hollow-pass warnings:

- Local harness (often git-excluded on this machine): `auditor-loop/AUDIT_PANEL.md`
- Operator notes: `auditor-loop/HOW_GROK_USES_THIS.md`

**no-mistakes** needs a feature branch with real commits ahead of `main`. Same tip as main → skipped steps, not “auditors accepted.”
