---
name: vantage-session-attribution
description: Apply the current Vantage classifier when the Vantage hook supplies a classifier file and turn reference.
---

When the hook supplies current attribution, reuse it unless the task, PR, ticket, or other classification changed. An unchanged turn requires no file read or tool call.

When the hook requests classification or the current values need correction, read the supplied classifier Markdown in full and follow its rules and allowed values. If the hook supplies fallback dimensions, use those. Call the bundled `set_turn_attribution` MCP tool with the supplied `session_id` and `turn_id`. Pass the complete classification as the `tags` object with string values. Omitted dimensions are cleared. Pass an empty object to clear all dimensions. If the classifier describes command-line arguments, express the same dimensions as object keys and values.

Do not send prompts, summaries, source code, tool output, credentials, personal data, or local file paths. If the hook did not supply a turn reference, do nothing. Usage reporting continues independently.
