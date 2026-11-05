"""Optional MCP entry point: expose the research CLI as agent tools.

Every tool shells out to the same CLI the humans/agents use, so there is exactly one
implementation and both entry points stay in sync. Run with:

    research-mcp            # stdio transport

Tools return the CLI's JSON envelope as text, which agent clients can parse directly.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

try:  # mcp >= 2 renamed FastMCP to MCPServer
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:
    try:
        from mcp.server.fastmcp import FastMCP as _Server
    except ImportError as exc:  # pragma: no cover - only when the mcp extra is missing
        raise SystemExit("Install the MCP extra first: pip install 'agent-research-tool[mcp]'") from exc

mcp = _Server("research-tool")


def _run(args: list[str]) -> str:
    command = [sys.executable, "-m", "research_tool.research", *args, "--json"]
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    payload = (completed.stdout or "").strip()
    try:
        return json.dumps(json.loads(payload), ensure_ascii=False)
    except ValueError:
        return json.dumps({
            "ok": False,
            "error": {"message": (completed.stderr or payload or "no output")[:2000],
                      "exit_code": completed.returncode},
        }, ensure_ascii=False)


@mcp.tool()
def doctor() -> str:
    """Check prerequisites and report which capabilities are ready, missing or optional."""
    return _run(["doctor"])


@mcp.tool()
def init(question: str, state: str, purpose: str = "", deliverable: str = "",
         channels: str = "web", max_depth: int = 3) -> str:
    """Create a research tree. `state` is the JSON file path that holds the whole tree."""
    args = ["init", question, "--state", state, "--purpose", purpose,
            "--deliverable", deliverable, "--channels", channels, "--max-depth", str(max_depth)]
    return _run(args)


@mcp.tool()
def set_node(state: str, node: str, queries: list[str] | None = None,
             counter_queries: list[str] | None = None, scroll: int | None = None) -> str:
    """Configure the queries (and optional scroll depth) for one node."""
    args = ["set-node", "--state", state, "--node", node]
    if queries:
        args.extend(["--queries", *queries])
    if counter_queries:
        args.extend(["--counter-queries", *counter_queries])
    if scroll is not None:
        args.extend(["--scroll", str(scroll)])
    return _run(args)


@mcp.tool()
def run(state: str, node: str = "", ready: bool = True, fetch: int = 3, scroll: int | None = None) -> str:
    """Collect evidence for one node or every open node. Returns a compact node summary."""
    args = ["run", "--state", state]
    args.extend(["--node", node] if node else ["--ready"])
    args.extend(["--fetch", str(fetch)])
    if scroll is not None:
        args.extend(["--scroll", str(scroll)])
    return _run(args)


@mcp.tool()
def show(state: str, node: str, chars: int = 1500) -> str:
    """Show a node's evidence with E# references and body previews."""
    return _run(["show", "--state", state, "--node", node, "--chars", str(chars)])


@mcp.tool()
def review(state: str, payload: dict) -> str:
    """Apply review verdicts (answered / partial / not_found, children, gaps) to nodes."""
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
    try:
        json.dump(payload, handle, ensure_ascii=False)
        handle.close()
        return _run(["apply-review", "--state", state, "--file", handle.name])
    finally:
        Path(handle.name).unlink(missing_ok=True)


@mcp.tool()
def claim(state: str, node: str, text: str, evidence: str, support: str = "single",
          counter: str = "") -> str:
    """Record a conclusion with its evidence references (E1,E2) and support level."""
    args = ["claim", "add", "--state", state, "--node", node, "--text", text,
            "--evidence", evidence, "--support", support]
    if counter:
        args.extend(["--counter", counter])
    return _run(args)


@mcp.tool()
def digest(state: str) -> str:
    """Render the seven-section research digest."""
    return _run(["digest", "--state", state])


@mcp.tool()
def status(state: str) -> str:
    """Show layers, pending nodes, scroll/PDF candidates and the next suggested command."""
    return _run(["status", "--state", state])


def main() -> None:
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
