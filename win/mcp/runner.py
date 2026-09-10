"""
Tool runner: executes a PowerShell tool script in -Json mode and returns
the parsed JSON result.

Every tool is invoked as:
    <powershell> -NoProfile -ExecutionPolicy Bypass -File <script> -Path <path> -Json -NoPause [extra args]

The script's stdout MUST be a single JSON object in -Json mode.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("mytools-mcp.runner")

# Configurable via environment variable; defaults to Windows PowerShell 5.1.
POWERSHELL_BIN = os.environ.get("MYTOOLS_POWERSHELL", "powershell.exe")


def run_tool(
    ps1_path: str | Path,
    path_arg: str,
    extra_args: Optional[List[str]] = None,
    timeout: int = 120,
) -> Dict[str, Any]:
    """
    Execute a PowerShell tool in JSON mode and return the parsed result.

    Args:
        ps1_path: Absolute path to the .ps1 script.
        path_arg: The target path passed as -Path.
        extra_args: Additional arguments (e.g. ["-Force"]).
        timeout: Maximum seconds to wait.

    Returns:
        Parsed JSON dict from stdout. If parsing fails, returns an error dict.
    """
    cmd = [
        POWERSHELL_BIN,
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", str(ps1_path),
        "-Path", path_arg,
        "-Json",
        "-NoPause",
    ]
    if extra_args:
        cmd.extend(extra_args)

    logger.debug("Running: %s", " ".join(cmd))

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.error("Tool timed out after %ds", timeout)
        return {"status": "error", "errors": [f"Tool timed out after {timeout}s"]}
    except FileNotFoundError:
        logger.error("PowerShell binary not found: %s", POWERSHELL_BIN)
        return {"status": "error", "errors": [f"PowerShell not found: {POWERSHELL_BIN}"]}

    stdout = proc.stdout.strip()

    # Strict parsing: in -Json mode stdout MUST be pure JSON.
    result = _parse_json_output(stdout)
    if result is None:
        logger.error(
            "Failed to parse JSON output (exit=%d). stdout[:200]=%s stderr[:200]=%s",
            proc.returncode, stdout[:200], proc.stderr[:200],
        )
        result = {
            "status": "error",
            "errors": [
                f"Tool did not produce valid JSON (exit code {proc.returncode})",
                f"stdout: {stdout[:500]}",
                f"stderr: {proc.stderr[:500]}",
            ],
        }

    result["_exit_code"] = proc.returncode
    return result


def _parse_json_output(stdout: str) -> Optional[Dict[str, Any]]:
    """
    Strict JSON parsing. In -Json mode the tool must output exactly one JSON
    object. We try direct parse first; if that fails, we look for the last
    complete JSON object (in case of stray leading whitespace).
    """
    if not stdout:
        return None

    # Direct parse
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        pass

    # Fallback: find the last '{' and try to parse from there (defensive only)
    idx = stdout.rfind("{")
    if idx >= 0:
        try:
            return json.loads(stdout[idx:])
        except json.JSONDecodeError:
            pass

    return None
