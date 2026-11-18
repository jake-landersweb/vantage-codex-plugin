---
name: vantage-session-attribution
description: Tag the current Codex task with purpose and project when the Vantage hook supplies a session-specific attribution command.
---

Use the exact session-specific attribution command supplied by the Vantage hook for the current Codex session.

Add:

- `--purpose research`, `development`, `design`, or `unclassified`
- `--project` only when the project is explicit; do not infer it from a team or repository
- `--source explicit`, `inferred`, or `manual`

Tag the session at the beginning of substantive work and whenever its purpose or project changes. Do not resend unchanged metadata every turn.

Do not send prompts, summaries, source code, tool output, credentials, personal data, or local file paths. If the hook did not supply an attribution command, do nothing. Usage reporting continues independently.
