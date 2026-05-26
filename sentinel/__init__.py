import importlib.metadata

try:
    __version__ = importlib.metadata.version("centauri-sentinel")
except importlib.metadata.PackageNotFoundError:
    __version__ = "0.1.0"
