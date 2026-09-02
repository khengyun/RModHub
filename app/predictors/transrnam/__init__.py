"""TransRNAm back-end (transformer + CNN, 601-nt window, the same 12 modifications)."""

from app.predictors.transrnam.predictor import TransRNAmPredictor

__all__ = ["TransRNAmPredictor"]
