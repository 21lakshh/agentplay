"""Small shared helpers."""

from __future__ import annotations

import contextlib
import logging

logger = logging.getLogger("agentplay")


class captured(contextlib.AbstractContextManager):  # noqa: N801 - reads like suppress()
    """Suppress and log any exception raised inside the block.

    Used on best-effort paths (metadata, enrichment) where one odd record must
    never fail a whole map build. The exception is logged at debug level so
    ``--verbose`` surfaces it.
    """

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            return False
        if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            return False
        logger.debug("suppressed %s: %s", exc_type.__name__, exc)
        return True
