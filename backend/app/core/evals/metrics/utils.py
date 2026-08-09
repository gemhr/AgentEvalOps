"""Shared utilities for evaluation metrics."""

from __future__ import annotations

import json
import textwrap

#: Short guidance for LLM judge prompts: Harness/meta-tool calls (like `harness_*`)
#: are infrastructure, not task attempts. Judge them as neutral—exclude from scoring,
#: do not penalize or reward, and only score actual task-directed actions.
HARNESS_TOOL_GUIDANCE = textwrap.dedent("""\
    Harness tools: the agent may run inside a harness that offers meta-tools
    for its own operation — retrieving learned guidance or rules, reading or
    acknowledging diagnostic notices, checking a rule's status, or inspecting an
    earlier trace. Names are commonly prefixed (for example `harness_*`), and
    such a call reads or acknowledges harness state rather than acting on the
    user's task.

    Judge these as NEUTRAL:
    - Do not treat them as incorrect, irrelevant, or mis-selected tools, and do
      not count them as unnecessary, redundant, or inefficient steps. Consulting
      guidance before acting is sanctioned behaviour, not a mistake.
    - Exclude them from the assessment rather than rewarding them. They earn no
      credit, and a trace containing only harness calls with no attempt at the
      user's task still scores poorly for the missing attempt.
    - A trace whose visible work is entirely harness calls is preparation
      within a longer task, not a failure: score the task-directed work present,
      and if there is none, say the trace shows no task-directed activity rather
      than that the wrong tools were chosen.
    - Judge only the task-directed actions on their own merits.
""")


def to_text(value: object) -> str:
    """Serialize a trace input/output value to a plain string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str)
