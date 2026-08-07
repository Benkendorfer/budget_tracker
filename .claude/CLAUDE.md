# Claude instructions

## Priorities

1. Keep core simulation logic separate from rendering/UI.
2. Prefer simple, testable code over clever abstractions.
3. Avoid premature optimization.
4. Do not introduce native C++ extensions unless explicitly requested.
5. Avoid large refactors. A small amount of tech debt is ok if it keeps individual changes small. Prefer to keep small amounts of tech debt and save them for independent refactoring steps later.

## Running work in parallel

Split parallel agents by **dependency, not by file**. Two agents may edit the same file
and be merged afterwards; what cannot be parallelised is work whose tests will not run
until another agent's module exists. An agent that cannot verify itself hands back
plausible untested code, which is worse than waiting for it.

Two consequences worth remembering:

- Merging cleanly is not the same as working. Independent changes can each be correct,
  merge without a conflict, and still not fit together — so the merged state has been
  tested by nobody and needs its own run.
- A git worktree contains only **committed** files, so uncommitted work is invisible to
  agents running in one.
- **The dispatcher must hold still while an agent is running.** An agent verifies itself
  with the whole suite, so editing *any* file — even one it was told not to touch — makes
  its runs disagree with each other. It cannot tell your edits from its own bug, so it
  re-runs, and re-runs. If something small needs doing mid-flight, either queue it or
  accept that the agent's test results are now unreliable and re-verify yourself
  afterwards.
