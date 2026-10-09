"""Tapetide Stock Research MCP — local stdio bridge to https://mcp.tapetide.com/mcp."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("tapetide-mcp")
except PackageNotFoundError:  # running from a source checkout without install
    __version__ = "0.0.0+local"
