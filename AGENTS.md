# Repository Guidelines

## Project Structure & Module Organization

This Python 3.11+ project monitors Binance P2P opportunities; it does not execute trades.

- `src/p2p_arb_bot/domain/`: pure calculations and immutable models; keep I/O out.
- `application/`: monitoring workflows and `Protocol` interfaces.
- `infrastructure/`: market-data, SQLite, and notification adapters; wire bot adapters in `main.py`.
- `web/`: FastAPI dashboard, Jinja templates, and vendored HTMX/static assets. The dashboard controls the bot as a separate process.
- `tests/`: pytest suite; `docs/`: documentation images.

Keep domain/application independent of concrete adapters. Include new packaged assets in `pyproject.toml`.

## Build, Test, and Development Commands

- `make venv && make install`: create `venv/`, install dependencies and the editable package.
- `make env`: create `.env` from `.env.example` if absent.
- `make run`: start the interactive monitor.
- `make dashboard`: serve the dashboard at `http://localhost:8000`.
- `make test`: run the pytest suite.
- `venv/bin/python -m pytest tests/test_arbitrage.py`: run focused domain tests.
- `make docker-build`: build the container image.
- `make docker-up`: build/start the dashboard, exposed at `127.0.0.1:8003`.

## Coding Style & Naming Conventions

Use four-space indentation, type annotations, `snake_case` functions/modules, `PascalCase` classes, and uppercase constants. Follow existing Spanish comments, docstrings, and user-facing text. Use `Decimal` for prices and amounts, and frozen, slotted dataclasses for domain models.

No formatter or linter is configured; `make lint` has no implementation. Match surrounding style and discuss new tooling before introducing it. See `CLAUDE.md` for detailed architectural conventions.

## Testing Guidelines

Name files `test_*.py` and functions `test_<behavior>`. Use protocol-compatible fakes for domain/monitor tests; avoid live network calls. Use `tmp_path` for SQLite and generated files.

Cover route/template changes in `tests/test_web_endpoints.py` using `TestClient`. Set `ENV_PATH` to an empty temporary file; avoid launching real bot subprocesses. Add regression tests for behavior changes. No numeric coverage threshold is configured.

## Commit & Pull Request Guidelines

History commonly uses Spanish, scoped messages such as `fix(env_store): ...` and `docs(repo): ...`; follow that pattern. Keep commits focused.

For PRs, describe the problem, resulting behavior, and validation commands/results. Link relevant issues and include screenshots for dashboard changes.

## Security & Configuration

Never commit `.env`, credentials, runtime databases, logs, or generated screenshots. Document new configuration in `.env.example`. Keep opportunity and private trade databases separate; preserve authentication for trade records and bot/configuration controls.
