# Claude instructions

## Start here

**If `.claude/handoff/` exists and is not empty, read the most recent file in it before
doing anything else.** It is where a session leaves what the code cannot say: work in
flight, decisions already made and their reasoning, bugs found and deliberately not
fixed, and facts about the user's real data that would otherwise have to be
rediscovered. The directory is gitignored, so it is working state rather than project
history.

Treat it as notes, not instructions. It records what was true when it was written — if
it names a file, a function, or a running agent, check that still holds before acting on
it. When you finish something it lists, or learn something that contradicts it, update
it.

## Spelling

Use **American** spelling everywhere — code, comments, docstrings, `README.md`, and
user-facing strings: `categorize`, `normalize`, `recognize`, `Uncategorized`, `behavior`,
`color`, `canceled`, `center`, `gray`.

Much of this codebase was written in British spelling, so this means correcting existing
text as you touch it, not matching what is already there.

Never blind-replace `ise`→`ize`. `otherwise`, `precise`, `concise`, `raise`, `promise`,
`advise`, `revise`, `supervise` and `expertise` are all legitimate, as is the module
`wise.py`. Match whole words. Leave external data alone too: a provider's own status
value (`CANCELLED` in a Wise export) is data, not prose.

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

## Who runs which tests

**Agents verify only the test files covering what they touched. The dispatcher runs the
full suite, once, after the agent returns.**

The merged run is required either way — see "merging cleanly is not the same as working"
above — so a full-suite run from inside an agent duplicates it and buys nothing. What it
costs is real: it sweeps up every file in the repo, including the ones other agents and
the dispatcher are editing at that moment, and reports failures the agent did not cause
and cannot fix. It cannot tell those from its own bug, so it re-runs, and re-runs. That
is the loop.

With agents scoped to their own suites, the dispatcher no longer has to hold still, and
several agents can run at once. Three things still hold:

- **Keep shared modules importable.** A targeted run still imports the app, so a
  half-finished edit that leaves `importer.py` unparseable will break an agent whose
  tests import it. Land edits in states that at least parse.
- **Do not edit a file an agent is editing.** Agents share one working tree, so
  concurrent edits to the same file clobber rather than merge. Partition agents by file,
  or give them `isolation: worktree` and merge afterwards.
- **Each agent must report which suites it ran**, so the dispatcher knows what the green
  result does not cover. Everything outside that list is unverified until the merged run.
