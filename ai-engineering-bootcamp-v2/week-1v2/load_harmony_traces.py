#!/usr/bin/env python3
"""
Load Harmony SMS bot sample pack (20 traces) into internship.traces table.

Transforms Harmony JSONL format → internship.traces schema with field mapping.

Usage:
    source .venv/bin/activate
    python3 load_harmony_traces.py path/to/harmony-apartments-20-traces.jsonl
"""

import sys
import json
from pathlib import Path
from datetime import datetime
import os

from sqlalchemy import create_engine, text
import sqlalchemy as sa
from sqlalchemy.orm import Session

# Database connection via db.py (loads .env.db-accounts automatically)
import db


def load_jsonl(filepath: str) -> list[dict]:
    """Parse JSONL file, return list of trace dicts."""
    traces = []
    with open(filepath, 'r', encoding='utf-8') as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                trace = json.loads(line)
                traces.append(trace)
            except json.JSONDecodeError as e:
                print(f"❌ JSON parse error on line {line_no}: {e}")
                print(f"   First 200 chars: {line[:200]}")
                sys.exit(1)
    return traces


def validate_trace(trace: dict, trace_no: int) -> bool:
    """Validate trace has required fields."""
    required = ['user_input', 'response']
    for field in required:
        if field not in trace or trace[field] is None:
            print(f"❌ Trace {trace_no}: missing or null field '{field}'")
            return False
    return True


def map_trace_to_db_row(trace: dict) -> dict:
    """
    Transform Harmony JSONL format → internship.traces schema.

    Harmony has: trace_id, channel, user_input, retrieved_context, tool_calls, assistant_output
    Database expects: id, endpoint_type, response (JSONB), etc.
    """

    # Harmony uses trace_id; database uses id
    trace_id = trace.get('trace_id')
    if not trace_id:
        raise ValueError("Missing required field: trace_id")

    # Infer endpoint_type from presence of tool_calls
    endpoint_type = 'agent' if trace.get('tool_calls') else 'ask'

    # Structure response as JSONB (Harmony has assistant_output string)
    response = {
        "answer": trace.get('assistant_output', ''),
        "status": "success",
        "citations": []
    }

    # Extract citations from tool_calls if present
    tool_calls_list = trace.get('tool_calls', [])
    if tool_calls_list and isinstance(tool_calls_list, list):
        for tool in tool_calls_list:
            if isinstance(tool, dict) and tool.get('result'):
                response['citations'].append({
                    "source": tool.get('name', 'tool'),
                    "snippet": str(tool.get('result', ''))[:200]
                })

    # Parse retrieved_context (Harmony has string; normalize to JSONB)
    retrieved_context = None
    if trace.get('retrieved_context'):
        retrieved_context = {
            "text": trace.get('retrieved_context'),
            "source": "knowledge_base"
        }

    # Infer rag_status
    rag_status = 'supported' if retrieved_context else 'not_applicable'

    # Build agent_trace from tool_calls
    agent_trace = None
    if tool_calls_list:
        agent_trace = {
            "steps": [
                {
                    "type": "observe",
                    "tool_calls": tool_calls_list
                }
            ]
        }

    return {
        'id': trace_id,
        'endpoint_type': endpoint_type,
        'user_input': trace.get('user_input', ''),
        'system_prompt': "Harmony SMS leasing bot",
        'response': json.dumps(response),  # JSONB
        'retrieved_context': json.dumps(retrieved_context) if retrieved_context else None,  # JSONB
        'rag_status': rag_status,
        'agent_trace': json.dumps(agent_trace) if agent_trace else None,  # JSONB
        'tool_calls': json.dumps(tool_calls_list) if tool_calls_list else None,  # JSONB
        'grounding': 'tool_sources' if tool_calls_list else 'no_tool_call',
        'model_turns': 1,
        'model': 'unknown',  # Harmony pack doesn't specify model
        'prompt_tokens': None,
        'completion_tokens': None,
        'total_tokens': None,
        'latency_ms': None,
        'cost_usd': None,
        'embedding_cost_usd': None,
        'source': 'harmony_sample',
        'channel': trace.get('channel', 'sms'),
        'created_at': datetime.utcnow(),
    }


def create_annotation_row(trace_id: str) -> dict:
    """Create empty annotation row for a trace."""
    return {
        'trace_id': trace_id,
        'open_code_notes': None,
        'failure_category': None,
        'pass_fail': None,
        'reason_if_fail': None,
        'analyst': None,
        'created_at': datetime.utcnow(),
        'updated_at': datetime.utcnow(),
    }


def load_harmony_pack(jsonl_path: str) -> tuple[int, int]:
    """
    Load Harmony sample pack into database with schema transformation.

    Returns: (num_traces_inserted, num_annotations_inserted)
    """
    filepath = Path(jsonl_path)

    if not filepath.exists():
        print(f"❌ File not found: {filepath.resolve()}")
        sys.exit(1)

    print(f"📂 Loading: {filepath.resolve()}")

    # Parse JSONL
    print("📖 Parsing JSONL...")
    traces = load_jsonl(jsonl_path)
    print(f"   ✓ Parsed {len(traces)} traces")

    if len(traces) != 20:
        print(f"⚠️  Warning: Expected 20 traces, got {len(traces)}")

    # Validate traces (Harmony format: need trace_id and user_input)
    print("✅ Validating Harmony format...")
    for i, trace in enumerate(traces, 1):
        if 'trace_id' not in trace or 'user_input' not in trace:
            print(f"❌ Trace {i}: missing trace_id or user_input")
            sys.exit(1)
    print(f"   ✓ All {len(traces)} traces valid")

    # Transform Harmony → DB schema
    print("🔄 Transforming Harmony format → database schema...")
    db_rows = []
    for i, trace in enumerate(traces, 1):
        try:
            db_row = map_trace_to_db_row(trace)
            db_rows.append(db_row)
        except ValueError as e:
            print(f"❌ Trace {i} transformation failed: {e}")
            sys.exit(1)

    # Insert into database (transactional)
    print("💾 Inserting into internship.traces...")
    engine = db.get_engine()

    try:
        with engine.begin() as conn:
            # Insert traces using raw SQL
            insert_sql = text("""
                INSERT INTO internship.traces
                (id, endpoint_type, user_input, system_prompt, response, retrieved_context,
                 rag_status, agent_trace, tool_calls, grounding, model_turns,
                 model, prompt_tokens, completion_tokens, total_tokens,
                 latency_ms, cost_usd, embedding_cost_usd, source, channel, created_at)
                VALUES
                (:id, :endpoint_type, :user_input, :system_prompt, :response, :retrieved_context,
                 :rag_status, :agent_trace, :tool_calls, :grounding, :model_turns,
                 :model, :prompt_tokens, :completion_tokens, :total_tokens,
                 :latency_ms, :cost_usd, :embedding_cost_usd, :source, :channel, :created_at)
            """)

            for row in db_rows:
                conn.execute(insert_sql, row)

            print(f"   ✓ Inserted {len(db_rows)} traces")

            # Create empty annotation rows
            print("💾 Creating empty rows in internship.trace_annotations...")
            annotation_sql = text("""
                INSERT INTO internship.trace_annotations
                (trace_id, open_code_notes, failure_category, pass_fail, reason_if_fail, analyst, created_at, updated_at)
                VALUES
                (:trace_id, NULL, NULL, NULL, NULL, NULL, now(), now())
            """)

            for row in db_rows:
                conn.execute(annotation_sql, {'trace_id': row['id']})

            print(f"   ✓ Created {len(db_rows)} annotation rows")
            print("✅ Transaction committed")

    except Exception as e:
        print(f"❌ Insert failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    return len(db_rows), len(db_rows)


def run_qa_checks() -> bool:
    """Run QA queries to verify load was successful."""
    engine = create_engine(EXTERNAL_DB_URL)

    print("\n" + "=" * 70)
    print("🧪 QA VERIFICATION")
    print("=" * 70)

    with engine.connect() as conn:
        # Q1: Trace count
        q1 = text("""
            SELECT COUNT(*) as count FROM internship.traces
            WHERE source='harmony_sample'
        """)
        result = conn.execute(q1).scalar()
        print(f"✓ Traces in DB (source='harmony_sample'): {result}/20")
        if result != 20:
            print(f"  ⚠️  Expected 20, got {result}")
            return False

        # Q2: Annotation count
        q2 = text("""
            SELECT COUNT(*) as count FROM internship.trace_annotations
        """)
        result = conn.execute(q2).scalar()
        print(f"✓ Annotations in DB: {result}/20")
        if result < 20:
            print(f"  ⚠️  Expected ≥20, got {result}")
            return False

        # Q3: Response JSON validity
        q3 = text("""
            SELECT jsonb_typeof(response) as type, COUNT(*) as cnt
            FROM internship.traces WHERE source='harmony_sample'
            GROUP BY jsonb_typeof(response)
        """)
        result = conn.execute(q3).fetchall()
        for row in result:
            print(f"✓ Response JSON type '{row[0]}': {row[1]} traces")

        # Q4: Sample trace
        q4 = text("""
            SELECT id, user_input,
                   response->>'answer' as answer
            FROM internship.traces
            WHERE source='harmony_sample'
            LIMIT 1
        """)
        result = conn.execute(q4).fetchone()
        if result:
            print(f"\n✓ Sample trace:")
            print(f"  ID: {result[0]}")
            print(f"  User input: {result[1][:60]}...")
            print(f"  Answer: {result[2][:60] if result[2] else 'N/A'}...")

        # Q5: No premature notes
        q5 = text("""
            SELECT COUNT(*) as count FROM internship.trace_annotations
            WHERE open_code_notes IS NOT NULL
        """)
        result = conn.execute(q5).scalar()
        print(f"\n✓ Annotations with notes already filled: {result}")
        print(f"  (Expected: 0 — Tagg fills these in Task 1.3)")

    return True


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 load_harmony_traces.py <path-to-jsonl>")
        print("\nExample:")
        print("  python3 load_harmony_traces.py syllabus/week4/harmony-apartments-20-traces.jsonl")
        sys.exit(1)

    jsonl_path = sys.argv[1]

    print("""
╔═══════════════════════════════════════════════════════════════════╗
║          W4-TRACE Task 1.2: Load Harmony Sample Pack             ║
╚═══════════════════════════════════════════════════════════════════╝
""")

    # Load and insert
    num_traces, num_annotations = load_harmony_pack(jsonl_path)

    # Run QA checks
    if run_qa_checks():
        print("\n" + "=" * 70)
        print("✅ TASK 1.2 COMPLETE")
        print("=" * 70)
        print(f"""
Summary:
  • Traces loaded: {num_traces}/20
  • Annotations created: {num_annotations}/20
  • Database state: ready for Task 1.3 (open-coding)

Next: Tagg can now begin Task 1.3 (open-code 20 traces with notes)
""")
        return 0
    else:
        print("\n❌ QA checks failed")
        return 1


if __name__ == '__main__':
    sys.exit(main())
