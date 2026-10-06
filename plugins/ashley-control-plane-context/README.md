# Ashley Control Plane context

An opt-in, read-only view of the existing canonical state, project registry and
writer reservations. Not a second runtime owner, queue, scheduler or database.

Uses native `register_system_prompt_section` for new/reset session context and
`pre_llm_call` for fresh per-turn observations on resumed/reconnected sessions.
The per-turn hook does not rewrite the frozen system prompt. Invalid or conflicting
current authority emits an explicit unverified result and blocks tools through
`pre_tool_call`. Reservations are never represented as physical running proof.

No credentials, provider state or transcripts are read. Only selected current
fields and source hashes are emitted. Historical product data is excluded.
This is scoped to Ashley CONTROL_PLANE with products FROZEN; not a generic product
bootstrap and not a replacement for per-task scope enforcement, OS sandboxing,
shared lifetime writer locks, independent review or native health certification.

Install only under Ashley's existing profile after tests/review. Enable by adding
`ashley-control-plane-context` to that profile's existing plugins.enabled list,
retaining every other plugin and all provider settings. Rollback removes this one
entry and this plugin's own files; never restores an entire stale profile config.
A running agent may need native plugin rediscovery/new session; source presence
alone is not proof that the current live prompt consumed it.
