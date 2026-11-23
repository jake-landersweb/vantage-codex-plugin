---
name: vantage-session-attribution
description: Apply the current Vantage classifier when the Vantage hook supplies a classifier file and turn-specific attribution command.
---

Read the classifier Markdown file supplied by the Vantage hook in full. Treat it as the source of truth for classification instructions and allowed values.

Use the exact turn-specific attribution command supplied by the hook. Append only flags directed by the classifier. Do not infer allowed values or classification rules from this skill.

Apply the classifier to each new turn and update that turn when its classification changes.

Do not send prompts, summaries, source code, tool output, credentials, personal data, or local file paths. If the hook did not supply an attribution command, do nothing. Usage reporting continues independently.
