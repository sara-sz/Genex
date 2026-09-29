"""pilot_runtime.workflows — the minimal fictional pilot workflows.

Two actors, one record type, four operations. Enough to prove that
authentication, authorization, persistence, audit and revision history compose
against real adapters. Not a product.
"""

from .child_context import ChildContextService, WorkflowOutcome

__all__ = ["ChildContextService", "WorkflowOutcome"]
