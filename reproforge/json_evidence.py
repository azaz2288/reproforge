"""Unambiguous JSON decoding for specifications and stored evidence."""

from __future__ import annotations

import json
import math
from typing import Any


def loads(content: str | bytes) -> Any:
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('JSON contains duplicate object keys')
            result[key] = value
        return result

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError('JSON contains a nonfinite number')
        return number

    def reject_constant(value):
        raise ValueError('JSON contains a nonfinite literal')

    try:
        return json.loads(content, object_pairs_hook=unique_object,
                          parse_float=finite_float, parse_constant=reject_constant)
    except RecursionError as exc:
        raise ValueError('JSON nesting exceeds decoder capacity') from exc


def equal(left: Any, right: Any) -> bool:
    """JSON equality without Python's True==1 / False==0 coercion."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(equal(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(equal(a, b) for a, b in zip(left, right))
    return left == right
