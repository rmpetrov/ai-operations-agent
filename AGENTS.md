# AI Operations Agent — coding guardrails

- The current milestone is a deterministic FastAPI + PostgreSQL backend. **Do not add LLM features**.
- Treat `docs/ARCHITECTURE.md` and tests as the source of current scope and invariants.
- Preserve Alembic migrations; never silently create database tables at app startup.
- No hardcoded credentials outside explicitly local `.env.example` and docker-compose example.
- Do not expose the unauthenticated MVP on a public network.
- Keep PRs focused, include an API test for each behavior change, and explain technical tradeoffs.
- Never claim PostgreSQL integration passed without running it with `TEST_DATABASE_URL`.
- Leave generated reports, test DBs, and secrets out of git.
