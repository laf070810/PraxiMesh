"""Strict JSON contract for JavaScript function requests, never a code executor."""

import math
import re

MAX_SAFE_INTEGER = 2**53 - 1
MAX_DEPTH = 32
MAX_NODES = 20000


def validate_json(value):
    nodes = 0

    def walk(item, depth):
        nonlocal nodes
        nodes += 1
        if depth > MAX_DEPTH or nodes > MAX_NODES:
            raise ValueError("JavaScript JSON input exceeds structural limits.")
        if item is None or type(item) in {bool, str}:
            return
        if type(item) is int:
            if abs(item) > MAX_SAFE_INTEGER:
                raise ValueError("JavaScript requires safe integers.")
            return
        if type(item) is float:
            if not math.isfinite(item) or (
                item == math.trunc(item) and abs(item) > MAX_SAFE_INTEGER
            ):
                raise ValueError("JavaScript requires finite numbers and safe integers.")
            return
        if type(item) is list:
            for child in item:
                walk(child, depth + 1)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                walk(child, depth + 1)
            return
        raise ValueError("JavaScript requires JSON values.")

    walk(value, 0)


def validate_request(source, function, args):
    if not isinstance(source, str) or not isinstance(function, str):
        raise ValueError("JavaScript source and function must be strings.")
    if not re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]{0,127}", function):
        raise ValueError("JavaScript function must be a global identifier.")
    if type(args) is not list or len(args) > 64:
        raise ValueError("JavaScript requires at most 64 positional arguments.")
    validate_json(args)
