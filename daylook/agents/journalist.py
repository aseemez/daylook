"""Journalist Agent (build step 4).

Turns the Analyst's labeled blocks into a readable daily writeup. Text-only — it never sees
images, only the structured blocks.

Split of labor, to keep numbers honest and the eval criteria satisfied by construction:
  * The **LLM writes the narrative** — a short, natural recap of the day's arc.
  * The **timeline and the stats are rendered deterministically** from the real blocks, so the
    writeup always contains time-bucketed entries (eval criterion 1) and a real time
    distribution / ranking (eval criterion 4), with accurate numbers the model can't drift.

The narrative call is cached on its exact input (blocks + any session refinements), so reruns
are free.
"""

from __future__ import annotations

from daylook import cache, config
from daylook.adk_utils import run_text_agent

PROMPT_VERSION = "journalist-v1"


# --- Deterministic rendering ----------------------------------------------------------

def _hhmm(iso: str) -> str:
    return iso[11:16]


def _format_timeline(blocks: list[dict]) -> str:
    lines = []
    for b in blocks:
        dur = b["duration_seconds"] // 60
        lines.append(
            f"- {_hhmm(b['start'])}–{_hhmm(b['end'])} — {b['label']} "
            f"({b['app_name']}, {dur}m)"
        )
    return "\n".join(lines)


def _format_stats(day: dict) -> str:
    total = day.get("total_tracked_seconds", 0) // 60
    cats = sorted(day.get("time_by_category", {}).items(), key=lambda x: -x[1])
    apps = sorted(day.get("time_by_app", {}).items(), key=lambda x: -x[1])
    cat_line = ", ".join(f"{k} {v // 60}m" for k, v in cats) or "—"
    app_line = ", ".join(f"{k} {v // 60}m" for k, v in apps[:5]) or "—"
    return (
        f"- Total tracked: {total}m\n"
        f"- By category: {cat_line}\n"
        f"- Top apps: {app_line}"
    )


# --- Narrative (LLM, cached) ----------------------------------------------------------

_INSTRUCTION = (
    "You are the Journalist in a personal work-journal system. Given a chronological list of "
    "labeled activity blocks from someone's workday, write a short, natural recap (3-5 "
    "sentences) of how the day went — the main threads of work, notable context switches, and "
    "what got accomplished. Write in a warm, matter-of-fact second person ('You started the "
    "morning...'). Do NOT invent activities or times beyond what the blocks state. Return only "
    "the recap prose, no headings or bullet lists."
)


def _blocks_as_text(blocks: list[dict], refinements: list[str] | None) -> str:
    lines = [
        f"{_hhmm(b['start'])}-{_hhmm(b['end'])} [{b['category']}] {b['label']} "
        f"({b['app_name']}, {b['duration_seconds'] // 60}m)"
        for b in blocks
    ]
    text = "Activity blocks:\n" + "\n".join(lines)
    if refinements:
        text += "\n\nActive user preferences to honor:\n" + "\n".join(f"- {r}" for r in refinements)
    return text


def _make_agent():
    from google.adk.agents import LlmAgent
    return LlmAgent(
        name="journalist",
        model=config.DEFAULT_MODEL,
        instruction=_INSTRUCTION,
    )


def write_narrative(
    day: dict,
    refinements: list[str] | None = None,
    use_cache: bool = True,
) -> str:
    """The cached LLM recap paragraph for a day's blocks."""
    blocks = day.get("blocks", [])
    if not blocks:
        return "No tracked activity for this day."

    message = _blocks_as_text(blocks, refinements)
    key = cache.make_key(PROMPT_VERSION, config.DEFAULT_MODEL, message)
    if use_cache:
        hit = cache.get(key)
        if hit is not None:
            return hit["narrative"]

    agent = _make_agent()
    narrative = run_text_agent(agent, message).strip()
    cache.set(key, {"narrative": narrative})
    return narrative


# --- Public entry point ---------------------------------------------------------------

def write_journal(
    day: dict,
    refinements: list[str] | None = None,
    use_cache: bool = True,
) -> str:
    """Compose the full daily writeup: narrative (LLM) + timeline + stats (deterministic)."""
    date = day.get("date") or "unknown date"
    blocks = day.get("blocks", [])
    if not blocks:
        return f"# Work journal — {date}\n\nNo tracked activity for this day."

    narrative = write_narrative(day, refinements=refinements, use_cache=use_cache)
    timeline = _format_timeline(blocks)
    stats = _format_stats(day)

    return (
        f"# Work journal — {date}\n\n"
        f"{narrative}\n\n"
        f"## Timeline\n{timeline}\n\n"
        f"## By the numbers\n{stats}\n"
    )
