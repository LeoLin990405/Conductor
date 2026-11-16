"""LSP client/manager absorbed from XiaoTie (MIT)."""

from .client import LSPClient, LSPConfig
from .manager import LSPManager, format_diagnostics
from .protocol import (
    Diagnostic,
    DiagnosticSeverity,
    DocumentUri,
    Location,
    Position,
    Range,
    TextDocumentIdentifier,
    detect_language_id,
)

__all__ = [
    "LSPClient",
    "LSPConfig",
    "LSPManager",
    "Diagnostic",
    "DiagnosticSeverity",
    "DocumentUri",
    "Location",
    "Position",
    "Range",
    "TextDocumentIdentifier",
    "detect_language_id",
    "format_diagnostics",
]
