"""Prompt templates for the Confidence metric."""

import json
import textwrap
from typing import Any

from app.core.evals.metrics.utils import HARNESS_TOOL_GUIDANCE


class ConfidenceTemplate:
    """Stateless container for prompt-building class methods."""

    @staticmethod
    def evaluate_confidence(trace: dict[str, Any]) -> str:
        """Build the confidence evaluation prompt from a serialised trace."""
        return textwrap.dedent(f"""\
            You are evaluating the **confidence** of an AI agent's behavior \
            within a single execution trace.

            Confidence measures whether the agent's actions were decisive, \
            appropriate, and well-founded given the input. Consider:

            - **Decisiveness**: Did the agent act without unnecessary hesitation \
            or contradictory steps?
            - **Appropriateness**: Were the actions relevant to the user's goal?
            - **Consistency**: Did the agent maintain a coherent strategy \
            throughout the trace?
            - **Indicators of low confidence**: hedging language, contradictions, \
            unnecessary retries, vague or incomplete outputs, repeated tool \
            calls with the same parameters, or abandoning a strategy mid-way.

            {HARNESS_TOOL_GUIDANCE}
            Consulting harness state is not hesitation, indecision, or a \
            contradictory step: checking learned guidance before acting is a \
            deliberate choice. Do not read it as hedging or as abandoning a \
            strategy, and judge decisiveness over the task-directed actions only.

            Return a JSON object with:
            - `confidence`: a float between 0.0 and 1.0 \
            (1.0 = fully confident and decisive, 0.0 = completely uncertain).
            - `reason`: a brief explanation for the score.

            Trace:
            {json.dumps(trace, indent=2, default=str)}

            JSON:
        """)

    @classmethod
    def get_prompt_preview(cls) -> dict[str, str]:  # noqa: D102
        sample_trace = {
            "trace_id": "TRACE_ID",
            "name": "sample-trace",
            "spans": [
                {
                    "name": "agent",
                    "kind": "AGENT",
                    "input": {"task": "USER_TASK"},
                    "output": {"result": "AGENT_OUTPUT"},
                }
            ],
        }
        return {"evaluate": cls.evaluate_confidence(sample_trace)}
