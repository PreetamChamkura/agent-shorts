## Batch judgment

You were given multiple independent clip candidates above, each under its own "### Candidate N of M" heading. Judge EACH ONE ENTIRELY ON ITS OWN MERITS, using every rule above exactly as written for a single clip — nothing about the rules changes because there happen to be several candidates in one call.

This is not a comparison and not a ranking:

- Do not pick "the best" candidate and reject the rest
- A strong candidate must never lower your bar for the others, and a weak one must never raise it
- It is fully expected and normal that 0, some, or all of the candidates get a real segment — there is no target count, and matching some fraction of candidates to a segment is not itself a goal
- Judge candidate 3 exactly as you would if it were the only clip you had ever seen, with zero knowledge that any others exist in this call

## Output

Return only a JSON object. No prose, no markdown fences, no preamble. One result object per candidate, **in the same order the candidates were given above**, each carrying its own 1-based `clip_index` matching its "### Candidate N of M" heading:

{
  "results": [
    {
      "clip_index": 1,
      "segments": [
        {
          "start": 0.0,
          "end": 34.6,
          "punchline_at": 31.2,
          "tone": "revealing",
          "loopable": false,
          "hook": "six-word description",
          "caption_title": "SHORT PUNCHY TITLE",
          "vo_script": "the narrator line, or null",
          "on_screen_caption": null,
          "vo_placement": "cold_open",
          "narration_mode_reason": "the first four seconds are him walking to the fridge, nothing to lose",
          "why": "setup at 2.0 establishing X, turn at 24.0, payoff at 31.2, reaction to 34.6",
          "confidence": 0.82,
          "thumbnail_text": "\"I DIDN'T EVEN SEE HIM\"",
          "suggested_title": "He ruled this out three weeks before it happened",
          "title_style": "concrete",
          "suggested_hashtags": ["#valorant", "#clutch", "#shroud"],
          "suggested_description": "Clip via Shroud on Twitch. #valorant #clutch #shroud"
        }
      ]
    },
    {
      "clip_index": 2,
      "segments": []
    }
  ]
}

`clip_index` costs nothing to answer and exists so a dropped or reordered result is detectable instead of silently mismatched to the wrong clip — echo back the exact number from that candidate's own heading. Every candidate submitted must get exactly one result object; never merge two candidates into one, never skip one silently.

Field rules for the fields inside each result's `segments` entry — identical to the single-clip rules, applied per candidate:
