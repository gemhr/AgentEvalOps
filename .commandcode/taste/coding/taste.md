# Coding Style Preferences

- Prefers a single typed owner/adapter for normalization and identity logic rather than scattered ad-hoc string handling or inline concatenation (e.g., all step_ref normalization must go through one `StepIdentity` adapter; explicitly forbids sprinkling `"task-" + x` across evaluators). Confidence: 0.8
- Prefers fail-closed validation on invalid inputs over lenient matching — no fuzzy match, no `.contains()`, no canonical-text search, no display-name fallback, no guessing from timestamps; invalid input is an error, not a heuristic. Confidence: 0.8
- Prefers decoupling via a frozen wire/identity contract mirrored in the consuming repo plus cross-repo contract tests to verify both sides agree, rather than importing the production repo's internals as a runtime dependency for evaluation. Confidence: 0.8
