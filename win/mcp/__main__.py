"""
Entry point for `python -m mcp`.

Usage from the framework root:
    python -m mcp
or:
    python -m mcp.server
"""

from .server import main

if __name__ == "__main__":
    main()
