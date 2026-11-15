# Repository Guidelines

## Project Structure & Module Organization

The Python package lives in `engine/src/crucible/`: `core/` defines task interfaces and execution, `server/` provides HTTP/MCP services, and `export/` handles Harbor packaging. Built-in environments (`oncall`, `supplygym`) live under `envs/<name>/`, with their own policies and tests. Shared tests are in `engine/tests/`; the guess environment is a test fixture. The React/TypeScript workbench is in `web/src/`, with components in `components/` and tests in `web/tests/`. See `docs/` for guides and `configs/` for scoring configurations.

## Build, Test, and Development Commands

Use Python 3.14, uv, and Node.js 24 (matching CI). Run these commands from the repository root:

```sh
uv sync --directory engine --locked        # Install engine dependencies
uv run --directory engine crucible serve   # Start the API on port 8000
uv run --directory engine pytest           # Run shared and environment tests
uv run --directory engine ruff check       # Lint Python
uv run --directory engine ruff format --check
uv run --directory engine pyright          # Strict Python type checking
```

From `web/`, run `npm ci`, then `npm run dev` for Vite on port 5173, `npm test` for Node tests, and `npm run build` for TypeScript checks and production assets. From `engine/`, run `scripts/docker_smoke.sh` to validate Docker API and sidecar flows.

## Coding Style & Naming Conventions

Use four-space Python indentation, typed interfaces, and `snake_case` modules/functions. Ruff enforces imports and formatting with a 100-character line limit; Pyright runs in strict mode. Pre-commit hooks run Ruff and Pyright. Match existing TypeScript style: two-space indentation, single quotes, semicolons, and `PascalCase` component filenames. TypeScript strict checking is enabled.

## Testing Guidelines

Use pytest, pytest-asyncio, and Hypothesis; name Python tests `test_*.py`. Web tests use `*.test.mjs`. Add regression coverage for behavior changes, including determinism, score bounds, and hidden-state isolation where applicable. Environment policies must use tool results only. No numeric coverage threshold is configured. Regenerate golden scores through `CRUCIBLE_UPDATE_GOLDEN=1 uv run pytest tests/policies/test_golden.py` from `engine/`; never edit the JSON manually.

## Commit & Pull Request Guidelines

Follow the predominant history style: concise imperative subjects, such as `Add…`, `Fix…`, or `Harden…`; optional subsystem prefixes like `supplygym:` are established. PRs should describe behavior changes, link relevant issues, report validation commands/results, and include screenshots for UI changes. Run the applicable CI checks before requesting review.

## Security & Configuration

Keep `.env` files, generated runs, and Harbor exports out of Git. Harbor exports contain grade tokens; world salts and hidden simulator state must remain unavailable to evaluated agents.
