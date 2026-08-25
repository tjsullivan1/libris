# Project Instructions for AI Agents

@AGENTS.md

See also: [AGENTS.md](AGENTS.md)

## Claude-Specific Notes

- When starting on a new task, ask the user if a branch already exists. If it doesn't, create a new branch.
- When feature development/bug fix is complete, ask user if they would like a PR.
- Use GitHub Issues for issue tracking — reference issues in commits and PRs.
- When the user relays feedback from a code review, read the whole review before acting:
  `gh api repos/tjsullivan1/libris/pulls/<N>/comments` lists every inline comment. A relayed
  comment is usually one of several, and findings that share a root cause are best fixed
  together rather than one at a time.
- After a PR merges, check `git log` on `main`. Review suggestions accepted in the GitHub UI
  land as commits nobody in the session wrote, and later work is built on top of them.
