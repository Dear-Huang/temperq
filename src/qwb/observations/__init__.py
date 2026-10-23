"""Causal, revision-aware numeric observations for model adapters."""

from .windows import ObservationBatch, WindowBuilder

__all__ = ["ObservationBatch", "WindowBuilder"]
