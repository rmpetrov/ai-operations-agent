# AI Operations Agent — coding guardrails

- The current milestone permits the Sprint 4B offline, scripted fake-model runtime and deterministic evaluations alongside the FastAPI + PostgreSQL backend. **Do not integrate real LLM providers, credentials, or live model calls** until separately approved.
- Keep runtime tools read-only and advisory; no autonomous writes or public deployment until separately approved.
- Treat `docs/ARCHITECTURE.md` and tests as the source of current scope and invariants.
- Preserve Alembic migrations; never silently create database tables at app startup.
- No hardcoded credentials outside explicitly local `.env.example` and docker-compose example.
- Do not expose the unauthenticated MVP on a public network.
- Keep PRs focused, include an API test for each behavior change, and explain technical tradeoffs.
- Never claim PostgreSQL integration passed without running it with `TEST_DATABASE_URL`.
- Leave generated reports, test DBs, and secrets out of git.
