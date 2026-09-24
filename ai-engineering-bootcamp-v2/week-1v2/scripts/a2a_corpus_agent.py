"""p3m3 item #39, module 3.8 evidence -- a real A2A (Agent2Agent) server,
built by hand matching module-3.8.md's own toy-triage-agent example
structure, but wrapping this project's real search_corpus tool as the
published skill instead of a hardcoded stub response.

Real finding worth recording (see p3m3/week3-priority-checklist.md's D2
section): `pip install a2a-sdk` today resolves to 1.1.5, whose API has moved
to a protobuf-based `a2a_pb2.AgentCard` and FastAPI-route helpers
(`a2a.server.routes.add_a2a_routes_to_fastapi`) -- `a2a.server.apps` (the
module the syllabus's own example imports from) does not exist there at
all. Pinned `a2a-sdk==0.3.26` for this script specifically, the version the
syllabus's own class names (`A2AStarletteApplication`) actually match --
confirmed by import, not assumed. Deliberately NOT added to requirements.txt
(same scoping decision as scripts/mcp_corpus_server.py's `mcp<2` pin) --
curriculum-evidence-only, nothing in the deployed app imports it.

Run with: python scripts/a2a_corpus_agent.py  (serves on :9000)
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from dotenv import load_dotenv

load_dotenv(BASE / ".env")
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentSkill
from a2a.utils import new_agent_text_message
from openai import OpenAI

from rag_service import OVERFETCH_K, RAG_RELEVANCE_THRESHOLD, embed_query, get_collection, query_store, select_context_chunks

SKILL = AgentSkill(
    id="search-rag-corpus",
    name="Search the RAG corpus",
    description="Searches this project's own RAG corpus (260+ research papers and articles) and returns cited passages relevant to a question.",
    tags=["rag", "search"],
    examples=["What is the memory wall problem for mixture-of-experts models on SSDs?"],
)

CARD = AgentCard(
    name="Corpus Search Agent",
    description="Specialist agent that searches this project's RAG corpus on request from another agent.",
    url="http://localhost:9000/",
    version="1.0.0",
    capabilities=AgentCapabilities(streaming=True),
    skills=[SKILL],
    default_input_modes=["text"],
    default_output_modes=["text"],
)


class CorpusSearchExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        question = context.get_user_input()
        client = OpenAI(timeout=20.0)
        query_embedding, _ = embed_query(client, question)
        candidates = query_store(get_collection(), query_embedding, top_k=OVERFETCH_K)
        if not candidates["distances"] or candidates["distances"][0] > RAG_RELEVANCE_THRESHOLD:
            result = "No sufficiently relevant passages found in the corpus."
        else:
            retrieved = select_context_chunks(candidates)
            result = "\n\n".join(
                f"[{cid.rsplit('::', 1)[0]}] {doc[:600]}"
                for cid, doc in zip(retrieved["ids"], retrieved["documents"])
            )
        await event_queue.enqueue_event(new_agent_text_message(result))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("Cancellation not supported")


handler = DefaultRequestHandler(agent_executor=CorpusSearchExecutor(), task_store=InMemoryTaskStore())
app = A2AStarletteApplication(agent_card=CARD, http_handler=handler).build()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=9000)
