"""isotidy -- resolve overlapping annotations in Plant 3D isometric drawings."""

from .config import DEFAULT_ROLES, JP1071_ROLES, LayerRoles, Tuning
from .detect import Collision, Report, detect
from .extract import extract
from .model import Label, Scene
from .solve import SolveStats, solve
from .writeback import apply, save_dxf, sync_embedded_attribs

__all__ = [
    "DEFAULT_ROLES", "JP1071_ROLES", "LayerRoles", "Tuning",
    "Collision", "Report", "detect",
    "extract", "Label", "Scene",
    "SolveStats", "solve", "apply", "save_dxf", "sync_embedded_attribs",
]
