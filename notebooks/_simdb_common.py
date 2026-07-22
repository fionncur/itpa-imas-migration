"""Shared SimDB import/data-access modules for the notebooks."""

from typing import Any, Optional

import numpy as np
import numpy.typing as npt
from imas.ids_defs import EMPTY_FLOAT, EMPTY_INT

from simdb.config.config import Config
from simdb.database import get_local_db
from simdb.query import QueryType


def get_db():
    """Connect to the local SimDB."""
    return get_local_db(Config())


def guard(x: npt.ArrayLike) -> np.ndarray:
    """Replace the IMAS empty-float/empty-int sentinels with NaN."""
    a = np.asarray(x, dtype=float)
    is_empty = (np.abs(a) >= abs(EMPTY_FLOAT)) | (a == EMPTY_INT)
    return np.where(is_empty, np.nan, a)


def path(md: dict, *keys: str, n: int) -> np.ndarray:
    """Walk a nested meta_dict path"""
    node = md
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return np.full(n, np.nan)
        node = node[k]
    return guard(node)


def temp(md: dict, *names: str, n: int) -> np.ndarray:
    """First matching temporary-IDS quantity: db_variable.<name> or standard_name.<name>."""
    for bucket in ("db_variable", "standard_name"):
        for name in names:
            v = md.get(bucket, {}).get(name)
            if v is not None:
                return guard(v)
    return np.full(n, np.nan)


def temp_str(md: dict, *names: str, n: int) -> np.ndarray:
    """Like temp(), but for string-valued temporary-IDS quantities."""
    for bucket in ("db_variable", "standard_name"):
        for name in names:
            v = md.get(bucket, {}).get(name)
            if v is not None:
                return np.asarray(v, dtype=object)
    return np.full(n, "", dtype=object)


def path_str(md: dict, *keys: str, n: int) -> np.ndarray:
    """Like path(), but for string-valued quantities. Broadcasts a shot-scalar to length n."""
    node = md
    for k in keys:
        if not isinstance(node, dict) or k not in node:
            return np.full(n, "", dtype=object)
        node = node[k]
    arr = np.asarray(node, dtype=object)
    if arr.ndim == 0:
        return np.full(n, arr.item(), dtype=object)
    return arr


def query_selected(db, dataset: str, selec_key: str, machine: Optional[str] = None, value: str = "1") -> list[Any]:
    """Simulations for `dataset` (optionally restricted to `machine`) with at least one
    time-slice where db_variable.<selec_key> == value."""
    constraints = [("dataset", dataset, QueryType.EQ)]
    if machine is not None:
        constraints.append(("machine", machine, QueryType.EQ))
    constraints.append((f"db_variable.{selec_key}", value, QueryType.EQ))
    return db.query_meta(constraints)


def query_dataset(db, dataset: str, machine: Optional[str] = None) -> list[Any]:
    """All simulations for `dataset` (optionally restricted to `machine`), unfiltered by any
    selection flag."""
    constraints = [("dataset", dataset, QueryType.EQ)]
    if machine is not None:
        constraints.append(("machine", machine, QueryType.EQ))
    return db.query_meta(constraints)
