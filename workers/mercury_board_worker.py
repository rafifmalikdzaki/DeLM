"""Mercury-3 board worker: reason with Inception's diffusion LLM, coordinate via DeLM.

The worker reserves one board scope, asks Mercury-3
(https://api.inceptionlabs.ai/v1/chat/completions, OpenAI-compatible) for an
analysis/plan, publishes it as a FACT note, and completes the item with the
summary. It never writes model output to workspace files: code changes stay
with the human or the MCP coding agents.

Authentication: ``INCEPTION_API_KEY`` from the environment. A local ``.env``
file (``KEY=value`` lines, gitignored) in the current directory is loaded as
a fallback. The key is never printed, logged, or written anywhere.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp_server.server import DelmMcpServer

API_URL = "https://api.inceptionlabs.ai/v1/chat/completions"
DEFAULT_MODEL = "mercury-3"
DEFAULT_EFFORT = "low"
EFFORTS = ("instant", "low", "medium", "high")
TIMEOUT_SECONDS = 120


def load_dotenv(path: Path) -> None:
    """Fallback loader for a local .env file; real environment always wins."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def api_key() -> str:
    load_dotenv(Path.cwd() / ".env")
    key = os.environ.get("INCEPTION_API_KEY", "").strip()
    if not key:
        raise ValueError(
            "INCEPTION_API_KEY is not set: export it or add it to a local .env file"
        )
    return key


def chat_completion(
    key: str,
    messages: List[Dict[str, str]],
    model: str = DEFAULT_MODEL,
    reasoning_effort: str = DEFAULT_EFFORT,
    temperature: Optional[float] = None,
    max_tokens: int = 2000,
) -> Dict[str, Any]:
    """One Mercury-3 chat call; returns the parsed response body."""
    if reasoning_effort not in EFFORTS:
        raise ValueError(f"reasoning_effort must be one of {EFFORTS}")
    if temperature is not None and not 0.5 <= temperature <= 1.0:
        raise ValueError("Mercury-3 temperature must be between 0.5 and 1.0")
    payload: Dict[str, Any] = {
        "model": model,
        "reasoning_effort": reasoning_effort,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if temperature is not None:
        payload["temperature"] = temperature
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as stream:
            return json.load(stream)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:500]
        raise ValueError(f"Mercury API HTTP {error.code}: {detail}") from None
    except urllib.error.URLError as error:
        raise ValueError(f"Mercury API unreachable: {error.reason}") from None


def assistant_text(body: Dict[str, Any]) -> str:
    try:
        return body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise ValueError("Mercury API returned an unexpected shape") from None


ANALYZE_SYSTEM = (
    "You are a senior software engineer helping parallel agents coordinate. "
    "Be concrete and concise: name files, functions, and interfaces. "
    "Separate verified observations from guesses."
)


def run_worker(
    actor: str,
    board: Path,
    workspace: Path,
    subject: str,
    detail: str,
    model: str = DEFAULT_MODEL,
    reasoning_effort: str = DEFAULT_EFFORT,
    temperature: Optional[float] = None,
    max_tokens: int = 2000,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Reserve a scope, get Mercury-3's analysis, publish it, complete the item."""
    if not subject.strip() or not detail.strip():
        raise ValueError("subject and detail must be non-empty text")
    messages = [
        {"role": "system", "content": ANALYZE_SYSTEM},
        {"role": "user", "content": (
            f"Task scope: {subject}\nRequirements and context: {detail}\n\n"
            "Provide: (1) a short implementation plan with concrete steps, "
            "(2) likely pitfalls, (3) how to verify the result. "
            "Keep it under 600 words."
        )},
    ]
    if dry_run:
        return {"ok": True, "stage": "dry-run",
                "messages": messages, "model": model,
                "reasoning_effort": reasoning_effort}
    key = api_key()
    server = DelmMcpServer(
        actor=actor, board_path=board, workspace_path=workspace, peers=[actor]
    )
    reserve = server.call_tool(
        "delm_reserve_task", {"subject": subject, "detail": detail}
    )
    if reserve["isError"]:
        return {"ok": False, "stage": "reserve", "detail": reserve}
    item_id = reserve["content"][0]["text"].split("task `")[1].split("`")[0]
    body = chat_completion(key, messages, model, reasoning_effort, temperature,
                           max_tokens)
    analysis = assistant_text(body).strip()
    if not analysis:
        raise ValueError("Mercury API returned an empty analysis")
    usage = body.get("usage", {})

    note = server.call_tool("delm_publish_note", {
        "note_type": "FACT",
        "text": f"Mercury-3 analysis for '{subject}': {analysis[:1500]}",
        "scope": subject[:300],
        "basis": f"Single {model} ({reasoning_effort} effort) analysis call",
    })
    if note["isError"]:
        return {"ok": False, "stage": "note", "detail": note}
    done = server.call_tool("delm_complete_task", {
        "item_id": item_id,
        "summary": f"Mercury-3 plan published for '{subject}'. Full analysis in board note.",
    })
    if done["isError"]:
        return {"ok": False, "stage": "done", "detail": done}
    return {"ok": True, "stage": "done", "item_id": item_id, "usage": usage}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actor", default=os.environ.get("DELM_ACTOR", "mercury-1"))
    parser.add_argument("--board", default=os.environ.get(
        "DELM_BOARD_PATH", ".delm/board.sqlite"))
    parser.add_argument("--workspace", default=os.environ.get("DELM_WORKSPACE", "."))
    parser.add_argument("--subject", required=True, help="Scope to reserve on the board")
    parser.add_argument("--detail", required=True, help="Requirements/context for Mercury")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--reasoning-effort", choices=EFFORTS, default=DEFAULT_EFFORT)
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--max-tokens", type=int, default=2000)
    parser.add_argument("--dry-run", action="store_true",
                        help="Reserve nothing remote: print the request, send nothing")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_worker(
            actor=args.actor,
            board=Path(args.board),
            workspace=Path(args.workspace),
            subject=args.subject,
            detail=args.detail,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            temperature=args.temperature,
            max_tokens=args.max_tokens,
            dry_run=args.dry_run,
        )
    except ValueError as error:
        print(f"mercury-worker: {error}", file=sys.stderr)
        return 1
    if result.get("stage") == "dry-run":
        print(json.dumps({k: v for k, v in result.items() if k != "ok"}, indent=2))
        return 0
    if not result.get("ok"):
        print(f"mercury-worker: failed at {result.get('stage')}", file=sys.stderr)
        print(json.dumps(result.get("detail"), indent=2)[:2000], file=sys.stderr)
        return 1
    print(f"Completed {result['item_id']}; Mercury usage: {result.get('usage')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
