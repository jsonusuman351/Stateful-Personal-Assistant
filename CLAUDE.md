# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Before Implementing Anything

Always read the following docs in full before writing or modifying any code:

- `docs/SPEC.md` — functional requirements and scope
- `docs/DESIGN.md` — architecture decisions and system design
- `docs/TASKS.md` — current task list, priorities, and in-progress work

Do not skip this step even for small changes. Implementation decisions must align with the spec and design.

## Navigating the Codebase

Use `@filename` references to target specific files rather than scanning the entire codebase. When you need to understand a module, read it directly by path. Avoid broad recursive searches unless the target location is genuinely unknown.

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.11 |
| Agent framework | LangGraph |
| LLM / chain tooling | LangChain |
| API layer | FastAPI |
| State persistence | PostgreSQL |
| Caching | Redis |
| Deployment | Docker / Docker Compose |

## Commands

```bash
# Install dependencies (inside virtualenv or container)
pip install -r requirements.txt

# Start the full dev environment (API + DB + Redis)
docker compose up --build

# Start only the FastAPI server (assumes DB and Redis are running)
uvicorn app.main:app --reload --port 8000

# Run all tests
pytest

# Run a single test file
pytest tests/test_foo.py

# Run a single test by name
pytest tests/test_foo.py::test_bar

# Lint and type-check
ruff check .
mypy .
```

## Coding Standards

These are mandatory, not optional:

- **Type hints** — every function and method signature must include full type annotations (parameters and return type).
- **Docstrings** — every public function, method, and class must have a docstring. One-liners are fine for simple cases; use the Google style for complex ones.
- **Tests** — all business logic must have pytest tests. Tests live in `tests/` mirroring the source layout. No untested business logic ships.

## Architecture Overview

_Fill this in as the system is built. Key things to document here:_

- **Graph topology**: LangGraph node structure, edges, and conditional routing logic
- **State schema**: The shared state TypedDict passed between nodes
- **Persistence**: How PostgreSQL checkpointers are configured for LangGraph state
- **Caching layer**: What Redis stores and its TTL/invalidation strategy
- **API surface**: FastAPI routers, request/response models, and how they invoke the graph
