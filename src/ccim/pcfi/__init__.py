"""PCFI Layer - 4-compartment (S/D/U/R) split + Llama Guard 3 injection detection.

Design section 3.2.1: always run before compression. Target latency 50ms per request.
"""

from ccim.pcfi.compartments import Compartment, Compartments, Section
from ccim.pcfi.enforcer import PCFIAction, PCFIEnforcer, PCFIVerdict
from ccim.pcfi.llama_guard import GuardClient, GuardResult, LlamaGuardClient

__all__ = [
    "Compartment",
    "Compartments",
    "GuardClient",
    "GuardResult",
    "LlamaGuardClient",
    "PCFIAction",
    "PCFIEnforcer",
    "PCFIVerdict",
    "Section",
]
