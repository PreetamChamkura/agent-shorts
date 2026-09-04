.PHONY: test lint clip rerender

# Run the test suite. No network in tests.
test:
	uv run pytest

# Lint and check formatting.
lint:
	uv run ruff check .
	uv run ruff format --check .

# Full pipeline for one video: ingest -> transcribe -> select -> narrate -> render.
# Usage: make clip URL="https://youtu.be/..." ARGS="--start 412.8 --end 441.2 --tone confident"
clip:
	uv run clipper clip $(URL) $(ARGS)

# Re-run rendering only (stage 4). Must never re-download, re-transcribe, or re-call an LLM.
# Usage: make rerender ID=<video_id> ARGS="--start 412.8 --end 441.2 --tone confident --force"
rerender:
	uv run clipper render $(ID) $(ARGS)
