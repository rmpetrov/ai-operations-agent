# AI Operations Agent — coding guardrails

- The current milestone permits the Sprint 4B offline runtime and the Sprint 4C opt-in OpenAI Responses API adapter, verified exclusively through mocked provider responses, alongside the FastAPI + PostgreSQL backend. **Live model invocations and live credential provisioning remain prohibited until separately approved.** Any future approved live evaluation must use synthetic operational data only.
- Keep runtime tools read-only and advisory; no autonomous writes or public deployment until separately approved.
- No new agent HTTP endpoints, arbitrary tools/SQL, hosted provider tools, or orchestration frameworks. Keep the provider disabled unless trusted local code explicitly configures it; questions and tool arguments cannot enable it or configure models, credentials, or connection targets.
- Treat `docs/ARCHITECTURE.md` and tests as the source of current scope and invariants.
- Preserve Alembic migrations; never silently create database tables at app startup.
- No hardcoded credentials outside explicitly local `.env.example` and docker-compose example.
- Do not expose the unauthenticated MVP on a public network.
- Keep PRs focused, include an API test for each behavior change, and explain technical tradeoffs.
- Never claim PostgreSQL integration passed without running it with `TEST_DATABASE_URL`.
- Leave generated reports, test DBs, and secrets out of git.
