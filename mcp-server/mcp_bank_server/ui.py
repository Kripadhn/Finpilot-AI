"""MCP Apps UI resources (spec 2026-01-26).

Views are self-contained HTML files in apps/ served as ui:// resources with mimeType
`text/html;profile=mcp-app`. Tools link to them through `_meta.ui.resourceUri`; hosts push
tool results to the view over the bridge, so the view itself makes no network calls (UI-8).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
APPS_DIR = REPO_ROOT / "apps"

UI_MIME = "text/html;profile=mcp-app"
TRANSACTIONS_URI = "ui://bank/transactions"
SPENDING_URI = "ui://bank/spending-by-category"

_FILES = {
    TRANSACTIONS_URI: APPS_DIR / "transactions.html",
    SPENDING_URI: APPS_DIR / "spending-by-category.html",
}


def ui_tool_meta(resource_uri: str) -> dict:
    """Tool-definition metadata linking the tool to its view (MCP Apps 2026-01-26)."""
    return {"ui": {"resourceUri": resource_uri, "visibility": ["model", "app"]}}


def ui_resource_meta() -> dict:
    """Resource metadata: no external domains declared, host applies the restrictive CSP."""
    return {"ui": {"prefersBorder": True}}


def view_html(resource_uri: str) -> str:
    return _FILES[resource_uri].read_text(encoding="utf-8")
