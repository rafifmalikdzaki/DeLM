# Mercury-3 Board Worker

`workers/mercury_board_worker.py` lets Inception's Mercury-3 diffusion LLM
participate in DeLM coordination as a first-class board worker. One run works
a single scope the full DeLM way: it broadcasts status, reserves the scope
(reusing an already-open matching item instead of duplicating, and stopping
cleanly when a peer holds it or it is already done), asks Mercury-3 for an
implementation analysis, attaches the full analysis as a workspace file so
peers can inspect/apply it, publishes a `FACT` note pointer, and completes
the item with the file attached. Standard library only (`urllib`).

Run several workers in parallel with distinct `--actor` names on the same
`--board` file; the board's single-claim invariant keeps them from
duplicating scopes.

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
