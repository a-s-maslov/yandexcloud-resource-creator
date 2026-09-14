#!/usr/bin/env python3
"""
Centralized operation polling logic.
"""

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from exceptions import OperationError
from user_creator import UserCreator

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PollResult:
    """Completed operation counts from one polling pass."""

    successes: int = 0
    failures: int = 0


class OperationPoller:
    """Handles polling of Yandex Cloud operations."""

    def __init__(self, user_creator: UserCreator):
        self.user_creator = user_creator

    def poll_pending_operations(
        self, pending_ops: List[Dict[str, Any]], operation_type: str = "operation"
    ) -> PollResult:
        """Poll pending operations once and return successes and failures."""
        successes = 0
        failures = 0
        still_pending = []

        for item in pending_ops:
            op_id = item["operation_id"]
            operation_name = item.get("folder_name", item.get("database_name", "unknown"))

            try:
                data = self.user_creator.get_operation_status(op_id)
                done = data.get("done", False)

                if not done:
                    if self._has_operation_error(data):
                        self._log_operation_error(op_id, operation_name, data, operation_type)
                        failures += 1
                    else:
                        still_pending.append(item)
                    continue

                # Operation is done
                if self._has_operation_error(data):
                    self._log_operation_error(
                        op_id, operation_name, data, operation_type, is_final=True
                    )
                    failures += 1
                else:
                    self._log_operation_success(op_id, operation_name, item.get("start_time"))
                    successes += 1

            except OperationError as e:
                logger.error(f"Polling error for {operation_type} {op_id} ({operation_name}): {e}")
                failures += 1

        # Gentle pacing between poll cycles
        if still_pending:
            time.sleep(2)
        pending_ops[:] = still_pending
        return PollResult(successes=successes, failures=failures)

    def _has_operation_error(self, data: Dict[str, Any]) -> bool:
        """Check if operation has an error."""
        return "error" in data and data["error"]

    def _log_operation_error(
        self,
        op_id: str,
        operation_name: str,
        data: Dict[str, Any],
        operation_type: str,
        is_final: bool = False,
    ) -> None:
        """Log operation error consistently."""
        err = data["error"]
        status = err.get("code", "unknown")
        message = err.get("message", "no message")
        details = err.get("details", {})

        action = "failed" if is_final else "has failures"
        logger.error(
            f"YDB {operation_type} {op_id} for {operation_type} {operation_name} {action}: "
            f"status={status}, message={message}, details={details}"
        )

    def _log_operation_success(
        self, op_id: str, operation_name: str, start_time: Optional[float]
    ) -> None:
        """Log operation success consistently."""
        elapsed_info = ""
        if start_time:
            try:
                elapsed = time.time() - start_time
                elapsed_info = f" in {elapsed:.1f}s"
            except Exception:
                pass

        logger.info(
            f"YDB operation {op_id} for {operation_name} completed successfully{elapsed_info}"
        )
