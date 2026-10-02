"""Load an explicitly selected Python model frontend from any directory."""
import hashlib
import importlib.util
from pathlib import Path
import sys


def load_module(path):
    path = Path(path).resolve(strict=True)
    name = "search_model_" + hashlib.sha256(str(path).encode()).hexdigest()
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ValueError("model frontend must be a Python source file")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise
    return module
