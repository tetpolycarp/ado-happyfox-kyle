"""Root-level wrapper — delegates to src/integration/functions/ado_webhook."""
import logging
import traceback

import azure.functions as func

logger = logging.getLogger(__name__)

try:
    from src.integration.functions.ado_webhook import main  # noqa: F401
except Exception as exc:
    _startup_error = traceback.format_exc()
    logger.error("Failed to import ado_webhook: %s", _startup_error)

    def main(req: func.HttpRequest) -> func.HttpResponse:
        """Fallback — returns the import error for diagnosis."""
        return func.HttpResponse(
            f"Function startup error:\n\n{_startup_error}",
            status_code=500,
            mimetype="text/plain",
        )
