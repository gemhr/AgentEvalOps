# Workflow Preferences

- Runs development through a formalized, staged AI-coding workflow (task levels like H-2 architecture decision, H-3 implementation/execute, H-4 independent review, plus stage gates and work packages) driven by pasted structured prompt files; expects the agent to strictly honor each stage's scope (e.g., in architecture-decision rounds only freeze contracts, do not write implementation). Confidence: 0.9
- Assigns the agent a specific named role per round in a multi-agent pipeline (e.g., "本轮中你的角色是zcode"), alongside other agents like Codex / DeepSeek. Confidence: 0.7
- Prefers pushing work directly to the `main` branch — no feature branches or PRs — and expects user-added files (e.g., interview docs under `docs/interview`) to be included in the push. Confidence: 0.9
- Wants current state verified directly from code/bytes rather than trusting prior handoff/status documents — recompute sha256 provenance refs, re-audit the working tree, and never silently accept target drift or reuse stale refs. Confidence: 0.8
- No force-green: prefers honest evaluation results — real failures are preserved and reported rather than tuning thresholds or tests to fabricate a full PASS; evaluator defects are distinguished from true behavioral failures. Confidence: 0.85

