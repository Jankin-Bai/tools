#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MyTools MCP Server — exposes MyTools right-click tools as MCP (Model Context
Protocol) tools over stdio.

Protocol: JSON-RPC 2.0, newline-delimited JSON on stdin/stdout.
Diagnostics and logs go to stderr (via logging module).

Usage (MCP client config):
    {
      "command": "python",
      "args": ["-m", "mcp.server"],
      "cwd": "<framework-root>"
    }

Tools are discovered automatically from tools/*/tool.json where Mcp.Enabled
is true. Adding a new tool with Mcp metadata exposes it via MCP immediately.
"""

from __future__ import annotations

import json
import logging
import signal
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .registry import discover_tools, get_tool
from .runner import run_tool

SERVER_NAME = "mytools"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2024-11-05"

logger = logging.getLogger("mytools-mcp")

# JSON-RPC error codes
ERR_PARSE_ERROR = -32700
ERR_METHOD_NOT_FOUND = -32601
ERR_INTERNAL_ERROR = -32603


def _setup_logging() -> None:
    """Configure logging to stderr (stdout is the protocol channel)."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[%(name)s] %(levelname)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def send_response(msg_id: Any, result: Dict[str, Any]) -> None:
    """Send a JSON-RPC success response."""
    response = {"jsonrpc": "2.0", "id": msg_id, "result": result}
    print(json.dumps(response, ensure_ascii=False), flush=True)


def send_error(msg_id: Any, code: int, message: str) -> None:
    """Send a JSON-RPC error response."""
    response = {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": code, "message": message},
    }
    print(json.dumps(response, ensure_ascii=False), flush=True)


# ============================================================
# Handlers (Strategy pattern: method -> handler function)
# ============================================================

def handle_initialize(msg_id: Any, params: Dict[str, Any]) -> None:
    """MCP initialize handshake."""
    send_response(msg_id, {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    })
    logger.info("Initialized (protocol %s)", PROTOCOL_VERSION)


def handle_tools_list(msg_id: Any, params: Dict[str, Any]) -> None:
    """Return all MCP-enabled tool definitions."""
    tools = discover_tools()
    public_tools = [t.to_mcp_tool() for t in tools]
    send_response(msg_id, {"tools": public_tools})
    logger.info("tools/list: %d tool(s)", len(public_tools))


def handle_tools_call(msg_id: Any, params: Dict[str, Any]) -> None:
    """Execute a tool and return its result as MCP text content."""
    tool_name = params.get("name", "")
    arguments = params.get("arguments", {}) or {}

    tool = get_tool(tool_name)
    if tool is None:
        send_response(msg_id, {
            "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
            "isError": True,
        })
        return

    path_arg = arguments.get("path", "")
    if not path_arg:
        send_response(msg_id, {
            "content": [{"type": "text", "text": "Error: 'path' argument is required"}],
            "isError": True,
        })
        return

    # Build extra args from tool-specific parameters
    extra_args: list[str] = []
    if arguments.get("force"):
        extra_args.append("-Force")

    logger.info("tools/call: %s path=%s extra=%s", tool_name, path_arg, extra_args)

    try:
        result = run_tool(tool.ps1_path, path_arg, extra_args=extra_args)
    except Exception as e:
        logger.exception("Tool execution error")
        result = {"status": "error", "errors": [str(e)]}

    text = json.dumps(result, ensure_ascii=False, indent=2)
    is_error = result.get("status") in ("error",)
    send_response(msg_id, {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    })


# Handler registry: method name -> handler function
HANDLERS: Dict[str, Callable[[Any, Dict[str, Any]], None]] = {
    "initialize": handle_initialize,
    "tools/list": handle_tools_list,
    "tools/call": handle_tools_call,
}

# Notifications (no response expected)
NOTIFICATIONS = {"initialized", "notifications/initialized"}


# ============================================================
# Main loop
# ============================================================

def main() -> None:
    """Main JSON-RPC read loop with graceful shutdown."""
    _setup_logging()
    framework_root = Path(__file__).resolve().parent.parent
    logger.info("MyTools MCP server starting (framework: %s)", framework_root)

    def _shutdown(signum: int, frame: Any) -> None:
        logger.info("Received signal %d, shutting down", signum)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue

            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Invalid JSON: %s", line[:100])
                continue

            msg_id = message.get("id")
            method = message.get("method", "")
            params = message.get("params", {}) or {}

            try:
                if method in NOTIFICATIONS:
                    logger.debug("Notification: %s", method)
                    continue

                handler = HANDLERS.get(method)
                if handler is None:
                    logger.warning("Unknown method: %s", method)
                    if msg_id is not None:
                        send_error(msg_id, ERR_METHOD_NOT_FOUND, f"Method not found: {method}")
                    continue

                handler(msg_id, params)

            except Exception as e:
                logger.exception("Handler error for method=%s", method)
                if msg_id is not None:
                    send_error(msg_id, ERR_INTERNAL_ERROR, f"Internal error: {e}")

    except (KeyboardInterrupt, SystemExit):
        logger.info("Server shutting down")
    finally:
        logger.info("Server stopped")


if __name__ == "__main__":
    main()
