#!/usr/bin/env python3
"""p3m3 item #51 (A2): Human-in-the-loop agent with checkpointer and interrupt (module 3.2).

Demonstrates pause-before-consequential-action with LangGraph's interrupt() and
checkpointer. The agent lists staged document versions and can accept a new one,
but only after a human reviews the diff and approves.

Gate: accept_staged_version is a real, rare, auditable action with side effects
(updates the corpus), gated by INGEST_API_KEY auth + human approval.

Design:
- InMemorySaver checkpointer (local; Week 5 uses Postgres for persistence)
- Two tools: list_staged_versions (read-only) + accept_staged_version (consequential)
- accept_staged_version calls interrupt() with a preview, then resumes only after CLI approval
- CLI-based approval loop (not a web form; for dev/demo only)

To run: python scripts/hitl_agent_demo.py
        (requires .env with OPENAI_API_KEY + .env.db-accounts with DB settings)
"""
import os
import sys
import secrets
from pathlib import Path
from uuid import uuid4

# Project imports.
BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from dotenv import load_dotenv
load_dotenv(BASE / ".env")
load_dotenv(BASE / ".env.db-accounts")

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import MessagesState, StateGraph, START, END
from langgraph.prebuilt import ToolNode, tools_condition
from langchain_core.tools import ToolException


# Database access for staged versions (reuses project patterns).
def get_staged_versions() -> list[dict]:
    """Read staged document versions awaiting acceptance (read-only)."""
    from operational_store import get_engine
    from sqlalchemy import text

    engine = get_engine()
    with engine.connect() as conn:
        query = text("""
            SELECT d.id, d.title, dv.version,
                   char_length(dv.text) as new_chars,
                   char_length(d.text) as live_chars,
                   dv.created_at
            FROM internship.document_versions dv
            JOIN internship.documents d ON dv.document_id = d.id
            WHERE dv.accepted_at IS NULL
            ORDER BY dv.created_at DESC
        """)
        rows = conn.execute(query).fetchall()

    return [
        {
            "document_id": row[0],
            "title": row[1],
            "version": row[2],
            "new_size_chars": row[3],
            "live_size_chars": row[4],
            "staged_at": str(row[5]),
        }
        for row in rows
    ]


def get_version_diff(document_id: str, version: int) -> dict:
    """Get diff details for a staged version (read-only)."""
    from operational_store import get_engine
    from sqlalchemy import text
    import difflib

    engine = get_engine()
    with engine.connect() as conn:
        query = text("""
            SELECT d.text, dv.text, dv.provenance
            FROM internship.document_versions dv
            JOIN internship.documents d ON dv.document_id = d.id
            WHERE dv.document_id = :doc_id AND dv.version = :ver
        """)
        row = conn.execute(query, {"doc_id": document_id, "ver": version}).fetchone()

    if not row:
        return {"error": f"Version not found: {document_id} v{version}"}

    live_text, staged_text, provenance = row

    # Unified diff snippet (first 5 lines).
    diff_lines = list(difflib.unified_diff(
        live_text.splitlines()[:50],
        staged_text.splitlines()[:50],
        lineterm="",
        n=1
    ))[:10]

    # Similarity check.
    ratio = difflib.SequenceMatcher(None, live_text, staged_text).ratio()

    return {
        "document_id": document_id,
        "version": version,
        "similarity_ratio": round(ratio, 3),
        "live_size": len(live_text),
        "staged_size": len(staged_text),
        "diff_snippet": "\n".join(diff_lines),
        "provenance_present": bool(provenance),
    }


def accept_version_in_db(document_id: str, version: int) -> str:
    """Accept a staged version (consequential write; requires approval + auth)."""
    from operational_store import get_engine, record_event
    from sqlalchemy import text

    engine = get_engine()
    with engine.connect() as conn:
        # Mark as accepted.
        update_q = text("""
            UPDATE internship.document_versions
            SET accepted_at = NOW()
            WHERE document_id = :doc_id AND version = :ver
            RETURNING TRUE
        """)
        result = conn.execute(update_q, {"doc_id": document_id, "ver": version}).fetchone()
        conn.commit()

    if not result:
        return f"Accept failed: {document_id} v{version} not found or already accepted"

    # Log the action.
    record_event("ingest_version_accepted", {
        "document_id": document_id,
        "version": version,
        "accepted_by": "hitl-demo",
        "method": "agent-human-in-the-loop",
    })

    return f"Accepted {document_id} v{version}. The new version is now live."


AGENT_MODEL = "gpt-4.1-nano"
AGENT_SYSTEM_PROMPT = SystemMessage(content=(
    "You are an agent that reviews and accepts staged document versions. "
    "Use list_staged_versions to see what's pending. Use accept_staged_version "
    "to accept one, but only after reviewing the diff. Human approval is required; "
    "you will be interrupted and must wait for the human decision before writing. "
    "Treat the human's decision as final."
))


@tool
def list_staged_versions() -> str:
    """List all staged document versions awaiting human acceptance. Read-only."""
    versions = get_staged_versions()
    if not versions:
        return "No staged versions awaiting acceptance."

    lines = ["Staged versions:"]
    for v in versions:
        lines.append(
            f"  - {v['document_id']} v{v['version']}: {v.get('title', '(untitled)')[:40]} "
            f"({v['new_size_chars']} chars, staged {v['staged_at']})"
        )
    return "\n".join(lines)


@tool
def accept_staged_version(document_id: str, version: int) -> str:
    """Accept a staged version after human review and approval.

    This tool will interrupt and ask for human approval before proceeding.
    The human must provide the INGEST_API_KEY to authorize the write.
    """
    # Read-only preview (safe to re-run after interrupt).
    preview = get_version_diff(document_id, version)

    if "error" in preview:
        raise ToolException(preview["error"])

    # Interrupt: hand control to the human for review + approval.
    from langgraph.types import interrupt

    preview_text = (
        f"**Review: {document_id} v{preview['version']}**\n"
        f"Similarity: {preview['similarity_ratio']} (1.0 = identical, 0.0 = completely different)\n"
        f"Live: {preview['live_size']} chars → Staged: {preview['staged_size']} chars\n"
        f"Diff (first 10 lines):\n{preview['diff_snippet']}\n"
    )

    decision = interrupt({
        "action": "accept_staged_version",
        "preview": preview_text,
        "document_id": document_id,
        "version": version,
    })

    # After resuming: check approval and auth.
    if not decision.get("approved"):
        return f"Human reviewer rejected {document_id} v{version}. Not accepted."

    provided_key = decision.get("ingest_key", "")
    expected_key = os.getenv("INGEST_API_KEY", "")

    if not expected_key:
        # If no key is set, treat as not authenticated (fail-safe).
        return "INGEST_API_KEY not configured; cannot accept versions."

    if not secrets.compare_digest(provided_key, expected_key):
        return "Invalid INGEST_API_KEY. Version not accepted."

    # Auth + approval OK: actually accept.
    return accept_version_in_db(document_id, version)


def main():
    """Build and run the HITL agent."""
    print("[HITL Agent] Module 3.2 — pause before consequential action\n")

    # Build the graph with checkpointer.
    checkpointer = MemorySaver()
    llm = ChatOpenAI(model=AGENT_MODEL, timeout=20.0).bind_tools([
        list_staged_versions,
        accept_staged_version,
    ])

    def agent_node(state: MessagesState):
        return {"messages": [llm.invoke(state["messages"])]}

    graph = StateGraph(MessagesState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", ToolNode([list_staged_versions, accept_staged_version]))
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", tools_condition)
    graph.add_edge("tools", "agent")
    agent = graph.compile(checkpointer=checkpointer)

    thread_id = str(uuid4())
    config = {"configurable": {"thread_id": thread_id}}

    print(f"[Thread] {thread_id}\n")
    print("[Starting] Asking the agent to review staged versions...\n")

    # Initial question.
    initial_msg = HumanMessage(content=(
        "Review the staged document versions and accept any that look good. "
        "Let me review and approve each one before you accept it."
    ))

    # Run the agent.
    try:
        while True:
            print("[Agent] Thinking...\n")

            # Invoke the agent.
            for event in agent.stream(
                {"messages": [AGENT_SYSTEM_PROMPT, initial_msg]},
                config,
                stream_mode="updates",
            ):
                # On interrupt, handle the human approval flow.
                if "agent" in event:
                    pass  # Normal step; continue.

            # After the stream, check if we're at an interrupt.
            state = agent.get_state(config)
            if state.next and state.next != ("END",):
                # We're at an interrupt. Show the interrupt value and wait for input.
                next_node = state.next[0] if state.next else None

                if next_node == "__interrupt__":
                    interrupt_value = state.values.get("__interrupt__")
                    if interrupt_value:
                        print("\n" + "="*70)
                        print("[INTERRUPT] Human approval required")
                        print("="*70)
                        print(interrupt_value.get("preview", ""))

                        # CLI approval loop.
                        while True:
                            approval = input("Approve? (yes/no): ").strip().lower()
                            if approval in ("yes", "y"):
                                key = input(f"INGEST_API_KEY: ").strip()

                                # Resume with approval.
                                agent.stream(
                                    None,
                                    config | {"resume_value": {
                                        "approved": True,
                                        "ingest_key": key,
                                    }},
                                    stream_mode="updates",
                                )
                                break
                            elif approval in ("no", "n"):
                                # Resume with rejection.
                                agent.stream(
                                    None,
                                    config | {"resume_value": {"approved": False}},
                                    stream_mode="updates",
                                )
                                break
                            else:
                                print("Please enter 'yes' or 'no'.")
                else:
                    # Normal termination.
                    break
            else:
                # Stream completed without interrupt.
                break

            initial_msg = HumanMessage(content="Next version please.")

    except KeyboardInterrupt:
        print("\n[Interrupted] Demo ended by user.")
    except Exception as e:
        print(f"\n[Error] {e}")
        raise

    print("\n[Done] HITL demo complete.")


if __name__ == "__main__":
    main()
