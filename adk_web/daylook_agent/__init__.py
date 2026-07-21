"""Daylook agent package for the ADK dev UI (``adk web``).

Re-exports ``root_agent`` at package level so ADK discovers it whether it looks for
``daylook_agent.root_agent`` or ``daylook_agent.agent.root_agent``.
"""

from .agent import root_agent

__all__ = ["root_agent"]
