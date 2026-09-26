# AI Engineering Bootcamp v2

Hands-on course materials for building production-style LLM services with **FastAPI**, **OpenAI**, **Pydantic**, **PostgreSQL + pgvector**, **LangGraph**, and **Streamlit**.

## Weeks

| Week | Topic | Location |
|------|-------|----------|
| 1 | `/ask` endpoint — typed I/O, structured output, guardrails, model selection, cost | [`week-1/`](week-1/) |
| 1–3 (capstone) | **The capstone service**: Session 1 typed `/ask` with guardrails, Session 2 RAG (`/ingest`, `/debug/retrieve`, grounded + cited `/ask`, APA 7 references), Session 3 LangGraph agent (`/agent`) | [`week-1v2/`](week-1v2/) |
| 2 | RAG and vector databases (standalone class materials; the capstone's RAG lives in `week-1v2/`) | [`week-2/`](week-2/) |

## Tech stack

- **FastAPI** — HTTP API with automatic OpenAPI docs
- **OpenAI Python SDK** — chat completions and structured output (`response_format`)
- **Pydantic** — request/response schemas and validation guardrails
- **python-dotenv** — load `OPENAI_API_KEY` from `.env`
- **PostgreSQL + pgvector** — documents, embeddings, events and provenance
- **LangGraph** — the Session 3 agent loop
- **Streamlit** — the UI (`MVP_Layered_Ask.py` plus pages, including the Agent page)
- **httpx** — HTTP client for tests and the Streamlit UI

## Quick start

```bash
cd week-1v2
cp .env.example .env          # add OPENAI_API_KEY; put the DB_* settings in .env.db-accounts (Postgres is required)
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Requires Python 3.12. See [week-1v2/README.md](week-1v2/README.md) for the full capstone guide (setup, database, endpoints, deploy), or
[week-1/README.md](week-1/README.md) for the original five-stage version.
