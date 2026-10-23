"""Business processes, explicitly registered for the MAPP-D runtime and CLI."""

from typing import TYPE_CHECKING

from .installation import process as installation
from .rfq import process as rfq
from .support import process as support

if TYPE_CHECKING:
    from mappd.process import Process

PROCESSES: tuple[Process, ...] = (installation, rfq, support)
