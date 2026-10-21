# Mercury-3 Board Worker

`workers/mercury_board_worker.py` lets Inception's Mercury-3 diffusion LLM
participate in DeLM coordination as a board worker: it reserves one scope,
produces an implementation analysis, publishes it as a `FACT` note, and
completes the item. Standard library only (`urllib`).

## Usage

```bash
export INCEPTION_API_KEY=...   # or keep it in a gitignored local .env file
python3 workers/mercury_board_worker.py \
  --actor mercury-1 \
  --board /shared/project/.delm/board.sqlite \
  --workspace /shared/project \
  --subject "Add retry logic to uploader" \
  --detail "Inputs, expected interface, and constraints..."
```

Preview the exact Mercury request without spending or touching the board:

```bash
python3 workers/mercury_board_worker.py --subject S --detail D --dry-run
```

Options: `--model` (default `mercury-3`), `--reasoning-effort`
(`instant`/`low`/`medium`/`high`, default `low`),
`--temperature` (Mercury-3 allows 0.5–1.0), `--max-tokens` (default 2000).

## Cost and safety notes

- One run costs a single chat call (the live check used 1,483 tokens total).
- The key is read from `INCEPTION_API_KEY` at runtime and never printed,
  logged, or committed (`.env*` is gitignored). A key pasted anywhere else
  should be rotated.
- The worker never writes model output to workspace files: code changes stay
  with the human or the MCP coding agents. It publishes analysis text only.
- Offline tests (`tests/test_mercury_worker.py`) mock HTTP entirely, so the
  suite spends nothing.

## Verification

```bash
python3 -m unittest tests/test_mercury_worker.py -v
```
