"""Stage 3 — select (LLM).

Reads: work/{video_id}/words.json
Writes: work/{video_id}/segments.json

Uses prompts/select.md. Snaps start/end to word boundaries after the model
returns, and drops any segment whose `why` lacks two parseable timestamps.
Not part of milestone 1 (milestone 1 passes --start/--end manually).
"""
