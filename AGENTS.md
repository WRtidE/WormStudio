## Code Design Principles

### 1. Performance and Resource Efficiency

- Avoid unnecessary loops, repeated traversals, deep copies, and intermediate data structures.
- Prefer direct data access and lightweight transformations over redundant processing.
- Use deep copies only when independent mutable state is genuinely required.
- Do not introduce caching, batching, or other performance optimizations without a clear benefit.

### 2. Avoid Over-Defensive Programming

- Trust established upstream contracts and validated inputs.
- Do not repeatedly validate inputs or add fallback branches within internal methods when their preconditions are guaranteed by upstream logic.
- Avoid speculative exception handling, redundant null checks, and silent fallback values that conceal programming errors.
- Validate data at system boundaries; keep internal implementations focused on their explicit responsibilities.
- Add defensive handling only for realistic failure scenarios, not hypothetical misuse.

### 3. Clean and Maintainable Code

- Keep implementations simple, readable, and free of redundant logic, dead code, and unnecessary abstractions.
- Prefer straightforward control flow over excessive branching and deeply nested conditions.
- Centralize genuinely reusable utility functions in the appropriate `utils` directory.
- Search for and reuse existing utilities before introducing new ones.
- Do not create utility wrappers that merely delegate to an existing function without adding meaningful functionality.
- Remove obsolete code and unnecessary compatibility layers when they are no longer required.

### 4. Abstraction and Module Boundaries

- Place shared functionality at the lowest common architectural layer accessible to all intended consumers.
- When reusing functionality currently defined inside a specific module, evaluate whether it should be promoted to a shared module or an appropriate `utils` directory.
- Avoid cross-module dependencies on private implementation details.
- Keep domain-specific logic within its owning module; extract only functionality with genuine reuse potential.
- Preserve clear dependency direction and avoid circular dependencies.

## WormStudio Constraints

- Read [architecture](docs/ARCHITECTURE.md) and [development checks](docs/DEVELOPMENT.md) before changing the relevant code.
- Preserve existing uncommitted changes and recoverable historical results.
- Keep formal measurements, display-only estimates, automatic candidates, and human confirmations distinct; preserve the gates in `quality_schema.py`.
- Validate user input and saved/external files at their boundaries. Historical result compatibility and error recovery must have a concrete purpose.
- Keep platform-specific behavior in `src/platforms/` and maintain existing cache integrity checks.
- Retain independent mutable copies when queue jobs, review revisions, or worker threads need isolation.
- Install the CPU or CUDA environment with uv as described in `docs/INSTALL.md`; keep `pyproject.toml`, `uv.lock`, and `.python-version` together. Preserve the selected backend by using `uv run --no-sync` for daily commands.
- After relevant changes, run `uv lock --check`, `uv pip check`, `uv run --no-sync run_tests.py`, and the JavaScript tests in the development guide. Run `tools/check_source.py` on the clean source export; the checker intentionally rejects installed environments and runtime data.
