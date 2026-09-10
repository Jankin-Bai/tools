"""
Tool registry: scans tools/*/tool.json manifests and builds MCP tool definitions.

Only tools with "Mcp": {"Enabled": true} in their manifest are exposed via MCP.
The manifest is the single source of truth for tool metadata across all three
channels (right-click, CLI, MCP).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Immutable definition of a single MCP-exposed tool."""

    name: str
    description: str
    input_schema: Dict[str, Any]
    ps1_path: Path
    manifest: Dict[str, Any] = field(repr=False, hash=False)

    def to_mcp_tool(self) -> Dict[str, Any]:
        """Serialize to the MCP tools/list format."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


def get_framework_root() -> Path:
    """Resolve the framework root (parent of the mcp/ directory)."""
    return Path(__file__).resolve().parent.parent


@lru_cache(maxsize=1)
def discover_tools(tools_root: Optional[str] = None) -> List[ToolDefinition]:
    """
    Scan tools/*/tool.json and return MCP tool definitions for enabled tools.

    Results are cached for the lifetime of the process (lru_cache maxsize=1).
    To refresh after adding a tool, call discover_tools.cache_clear().
    """
    root = Path(tools_root) if tools_root else get_framework_root() / "tools"

    if not root.is_dir():
        return []

    tools: List[ToolDefinition] = []
    for tool_dir in sorted(root.iterdir()):
        if not tool_dir.is_dir():
            continue
        manifest_path = tool_dir / "tool.json"
        if not manifest_path.is_file():
            continue

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        mcp_cfg = manifest.get("Mcp") or {}
        if not mcp_cfg.get("Enabled", False):
            continue

        # Use the explicit Script field (single source of truth, no regex)
        script_name = manifest.get("Script")
        if not script_name:
            continue
        ps1_path = (tool_dir / script_name).resolve()
        if not ps1_path.is_file():
            continue

        tools.append(ToolDefinition(
            name=manifest["Name"],
            description=mcp_cfg.get(
                "Description", manifest.get("Description", manifest["DisplayName"])
            ),
            input_schema=mcp_cfg.get("InputSchema", _default_schema(manifest)),
            ps1_path=ps1_path,
            manifest=manifest,
        ))

    return tools


def get_tool(name: str) -> Optional[ToolDefinition]:
    """Look up a single tool by name."""
    for tool in discover_tools():
        if tool.name == name:
            return tool
    return None


def _default_schema(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Build a default inputSchema when the manifest doesn't specify one."""
    return {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": f"Absolute path to the target ({manifest.get('AppliesTo', 'file or folder')})",
            }
        },
        "required": ["path"],
    }
