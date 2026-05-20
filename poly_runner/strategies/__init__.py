"""Poly strategy registry."""
from .cl_predictor import CLPredictor
from .endgame import Endgame
from .maker_quote import MakerQuote
from .cross_asset import CrossAsset
from .reflexivity_emitter import ReflexivityEmitter


REGISTRY = {
    "cl_predictor":          CLPredictor,
    "endgame":               Endgame,
    "maker_quote":           MakerQuote,
    "cross_asset":           CrossAsset,
    "reflexivity_emitter":   ReflexivityEmitter,
}
