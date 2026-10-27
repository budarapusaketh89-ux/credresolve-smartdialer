"""Outbox worker entry point.

Run with: python -m app.worker
The worker keeps running after transient database/dispatch exceptions. Individual
provider failures are returned and logged by dispatch_outbox itself.
"""
import logging
import time

from .database import SessionLocal
from .services import dispatch_outbox

logger = logging.getLogger(__name__)
IDLE_SLEEP_SECONDS = 1.0
ERROR_SLEEP_SECONDS = 2.0


def main() -> None:
    while True:
        try:
            with SessionLocal() as db:
                result = dispatch_outbox(db)

            for error in result.get("errors", []):
                logger.warning(
                    "Provider outbox call %s attempt %s (%s): %s",
                    error.get("call_id"),
                    error.get("attempt"),
                    error.get("status", "unknown"),
                    error.get("error", "unknown error"),
                )

            if not result.get("sent") and not result.get("retrying") and not result.get("failed"):
                time.sleep(IDLE_SLEEP_SECONDS)
        except KeyboardInterrupt:
            logger.info("SmartDialer outbox worker stopping.")
            return
        except Exception:
            # Do not terminate the long-running worker on a transient DB or
            # dispatch error, and avoid a tight retry loop during an outage.
            logger.exception("Outbox worker iteration failed; will retry.")
            time.sleep(ERROR_SLEEP_SECONDS)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    main()
