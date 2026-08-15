"""layers/ — the five build-on-top layers (charter §10).

cua-driver gives perception + action. These layers are the agent's brain:
  A. goal      — NL goal → ordered app-level subgoals
  B. perception— filter AX markdown into LLM-actionable form
  C. sequencing— scan-act-verify loop, re-scan invariant
  D. recovery  — reflow / modal / crash handling
  E. vision    — screenshot → set-of-marks → V9 vision → click

Each layer is a small, testable module. The engine wires them into the
cascade. Cost knobs are explicit per layer.
"""
from .goal import decompose_goal, Subgoal
from .perception import filter_ax_markdown, extract_rows, element_count
from .sequencing import scan_act_verify, TurnResult
from .recovery import RecoveryPolicy
from .vision import VisionFallback, draw_set_of_marks
from .extract import try_extract, read_document_text, read_clipboard, read_field_value
from .deterministic import try_deterministic, DeterministicPlan

__all__ = [
    "decompose_goal", "Subgoal",
    "filter_ax_markdown", "extract_rows", "element_count",
    "scan_act_verify", "TurnResult",
    "RecoveryPolicy",
    "VisionFallback", "draw_set_of_marks",
    "try_extract", "read_document_text", "read_clipboard", "read_field_value",
    "try_deterministic", "DeterministicPlan",
]
