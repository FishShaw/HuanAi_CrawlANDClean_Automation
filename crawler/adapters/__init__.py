"""模板适配器。每个模块提供 iter_pages()；可选 fetch_detail() / download()，没有则走通用解析。"""
from __future__ import annotations

import importlib
from types import ModuleType


def get(name: str) -> ModuleType:
    return importlib.import_module(f".{name}", __name__)
