"""Stage 3.5 — narrate (TTS).

Reads: work/{video_id}/segments.json
Writes: work/{video_id}/vo_{n}.wav (absent when vo_script is null)

Cache by sha256 of the script text; never pay twice for the same line. On TTS
failure, flag it in segments.json and let the render proceed without VO rather
than failing the job. Not part of milestone 1.
"""
