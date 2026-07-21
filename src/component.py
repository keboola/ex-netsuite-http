"""NetSuite HTTP Extractor — component entrypoint (shell; filled in Phase E)."""

import logging

from keboola.component.base import ComponentBase
from keboola.component.exceptions import UserException


class Component(ComponentBase):
    """NetSuite HTTP extractor component."""

    def __init__(self):
        super().__init__()

    def run(self):
        """Main execution — thin orchestrator (implemented in Task 11)."""
        raise UserException("Not implemented yet")


if __name__ == "__main__":
    try:
        comp = Component()
        comp.execute_action()
    except UserException as exc:
        logging.exception(exc)
        exit(1)
    except Exception as exc:
        logging.exception(exc)
        exit(2)
