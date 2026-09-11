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
- **Fix the class, not the instance.** A review names one site; the same mistake is usually in
  two more. Before replying, look for the others — this is ADR 0028's whole subject, and every
  round of review that found "one more spelling" of an already-fixed defect could have been
  the last one instead.
- **An issue's stated diagnosis is a hypothesis.** Measure before building on it. In one week,
  issues here have named the wrong cause, the wrong count, the wrong file and the wrong fix —
  #99's proposed fix would have changed nothing, #100 had its premise backwards, #94 blamed a
  command that does not do the thing, and #78's numbers had drifted. Correct the issue in a
  comment when this happens; the next reader deserves the finding, not just the fix.

## The Bash tool mangles backslashes in heredocs here

Writing a heredoc through the Bash tool collapses `\\n` to a real newline, even when the
delimiter is quoted (`<<'EOF'`). It silently corrupts generated Python, `sed` scripts and
commit message bodies — the failure shows up later as a `SyntaxError` on an unterminated
string, or a commit message missing a word that got command-substituted away.

Use the Write and Edit tools for anything containing a backslash or a backtick. For commit
messages, write the body to a file and use `git commit -F`, or keep backticks out of the text.
