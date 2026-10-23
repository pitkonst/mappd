"""MAPP-D: Model gateway, Agents, Persistence, Processes, Durable execution."""

from mappd.durable import step, workflow
from mappd.process import MockSpec, Process

__version__ = "0.0.1"
__all__ = ["MockSpec", "Process", "__version__", "step", "workflow"]
