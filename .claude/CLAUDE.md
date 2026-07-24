# Claude instructions

## Priorities

1. Keep core simulation logic separate from rendering/UI.
2. Prefer simple, testable code over clever abstractions.
3. Avoid premature optimization.
4. Do not introduce native C++ extensions unless explicitly requested.
5. Avoid large refactors. A small amount of tech debt is ok if it keeps individual changes small. Prefer to keep small amounts of tech debt and save them for independent refactoring steps later.
