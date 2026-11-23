---
name: vantage-session-attribution
description: Apply the current Vantage classifier when the Vantage hook supplies a classifier file and turn-specific attribution command.
---

When the hook supplies current attribution, reuse it unless the task, PR, ticket, or other classification changed. An unchanged turn requires no file read or tagging command.

When the hook requests classification or the current values need correction, read the supplied classifier Markdown in full and follow its rules and allowed values. If the hook supplies fallback dimensions, use those. Use the hook's turn-specific command with `--replace` and the complete set of applicable `name=value` arguments. Omitted dimensions are cleared. Pass no values to clear all dimensions.

Do not send prompts, summaries, source code, tool output, credentials, personal data, or local file paths. If the hook did not supply an attribution command, do nothing. Usage reporting continues independently.
