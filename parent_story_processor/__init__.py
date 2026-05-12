"""Root-level wrapper — delegates to src/integration/functions/parent_story_processor."""
import logging
import traceback

import azure.functions as func

logger = logging.getLogger(__name__)

try:
    from src.integration.functions.parent_story_processor import main  # noqa: F401
except Exception as exc:
    _startup_error = traceback.format_exc()
    logger.error("Failed to import parent_story_processor: %s", _startup_error)

    def main(timer: func.TimerRequest) -> None:
        """Fallback — logs the import error."""
        logger.error("parent_story_processor startup error: %s", _startup_error)
