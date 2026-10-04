# AI Internship - Code Repository

This repository contains code snippets, notebooks, and exercises for various AI courses and programs offered through the AI Internship.

## 📚 Available Courses

### AI Engineering Bootcamp v2
Build a typed LLM service step by step: FastAPI + structured output (Session 1), RAG with PostgreSQL/pgvector and APA 7 citations (Session 2), LangGraph agent with tool orchestration (Session 3), and a TRACE evaluation system (Session 4).

**Sessions 1–3 (Weeks 1–3):** complete.
**Session 4 (Week 4): TRACE evaluation system** — in progress; the Maven submission is due Oct 7, 2026.
- Phases 1–4: discover failure patterns in 20 traces (the course-provided Harmony Apartments set), codify them as three deterministic checks, run the baseline, and build and measure a targeted fix.
- Measured baseline (2026-10-01): 6 of 20 traces pass all three checks (30%), which is below the 85% ship threshold, so the decision is BLOCK. The per-check results are 8/20, 17/20 and 18/20.
- Fix: a deterministic post-generation grounding gate (`grounding_gate.py`). Measured re-run (2026-10-04): 20/20 by the same three checks, "SHIP by checks, see caveats". Caveats: the gate was designed on the same 20 traces (overfitting risk, no held-out set), and ungrounded replies are replaced wholesale. Earlier "60%" and "85% after fix" figures were simulated, never measured, and are retracted.
- Status (2026-10-04): code pushed and deployed, including the Trace Eval page and debug-key gating. Screenshots and the post are still to do.
- Phase 5 (Ship): Maven submission.

- **Location:** `ai-engineering-bootcamp-v2/week-1v2/`
- **Planning:** kept in a local, gitignored planning record (`p3m3/`) that is not part of this public repository.
- **Get Started:** see [ai-engineering-bootcamp-v2/README.md](ai-engineering-bootcamp-v2/README.md)

### AI Builders Bootcamp
Practical builder sessions for agent workflows, AI evals, and production tooling.

- **Location:** `ai-builders-bootcamp/`
- **Status:** AI Evals Session and Week 3 examples available
- **Get Started:** See [ai-builders-bootcamp/README.md](ai-builders-bootcamp/README.md)

### Multi-Agent Systems
Learn to build production-ready multi-agent systems using LangGraph.

- **Location:** `multi-agent-systems/`
- **Status:** Weeks 1–4 available
- **Get Started:** See [multi-agent-systems/README.md](multi-agent-systems/README.md)

### VERA: Capstone Project
Verifiable Evidence-based Research Answers: a provenance-grounded research assistant that aims to produce auditable, evidence-backed answers.

VERA is the bootcamp capstone. It is a separate project in its own repository and is not included here; the weekly course assignments (Sessions 1–4) were built in `ai-engineering-bootcamp-v2/week-1v2/`. It is under active development; nothing here documents its status or schedule.

### AI Engineering Bootcamp (v1)
Earlier bootcamp modules — RAG, ADK/LangGraph agents, eval monitoring, and more.

- **Location:** `ai-engineering-bootcamp/`
- **Get Started:** see the README in each module folder where one exists

### Other folders
`claude-architect-bootcamp/` (see its [README](claude-architect-bootcamp/README.md)), `ai-portfolio-bootcamp/` and `lightning-lesson-demos/` hold further course material.

## 🗂️ Repository Structure

```
AI-Internship/
├── README.md                         # This file
├── ai-engineering-bootcamp-v2/       # AI Engineering Bootcamp v2
│   ├── week-1/                       # Original FastAPI /ask demo
│   ├── week-1v2/                     # Course service (/ask, RAG, /agent, Week 4 trace eval)
│   └── week-2/                       # RAG and vector databases
├── ai-builders-bootcamp/             # AI Builders sessions
│   ├── ai-evals-session/             # LLM tracing and eval tooling
│   └── week-3/                       # n8n agent workflow examples
├── ai-engineering-bootcamp/          # AI Engineering Bootcamp v1 modules
├── multi-agent-systems/              # Multi-Agent Systems course
│   └── week-1/ ... week-4/
├── claude-architect-bootcamp/        # Claude architect course material
├── ai-portfolio-bootcamp/            # Portfolio starter repo
└── lightning-lesson-demos/           # Short demo projects
```

## 🚀 Quick Start

1. **Choose a course** from the list above
2. **Navigate to the course directory**
3. **Read the course README** for specific instructions
4. **Follow the week-by-week structure**

## 📋 General Prerequisites

- Python 3.12 for the AI Engineering Bootcamp v2 course service (`week-1v2/`); other courses list their own requirements
- pip (Python package manager)
- Jupyter Notebook or JupyterLab (only for courses that ship notebooks)
- Git (for cloning this repository)

## 🔗 Resources

- [AI Internship Website](https://theaiinternship.com)
- Course-specific resources are listed in each course's README

## 📝 How to Use This Repository

1. **Clone the repository:**
   ```bash
   git clone <repository-url>
   cd AI-Internship
   ```

2. **Navigate to your course:**
   ```bash
   cd ai-engineering-bootcamp-v2/week-1v2
   ```

3. **Follow the course-specific instructions** in each week's README

4. **Work through the course material** in order, then experiment and learn

## 🆘 Getting Help

- Check the README in each week's folder
- Refer to course documentation
- Ask questions in the course discussion forum

## 📄 License

This repository contains educational materials for the AI Internship program.

---

**Welcome to the AI Internship!**

