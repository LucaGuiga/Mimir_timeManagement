"""Configuration loader. Everything reads config/config.yaml through here."""
import os
import threading

import yaml
from ruamel.yaml import YAML

_lock = threading.Lock()
_cache = {}

REQUIRED_KEYS = [
    "canvas.token", "github.pat", "oura.pat", "anthropic.api_key",
    "telegram.bot_token", "telegram.chat_id",
    "mysql.host", "mysql.port", "mysql.user", "mysql.password", "mysql.db",
]


class ConfigError(Exception):
    pass


def repo_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def default_config_path():
    return os.path.join(repo_root(), "config", "config.yaml")


def load_config(path=None):
    path = os.path.abspath(path or default_config_path())
    with _lock:
        if path in _cache:
            return _cache[path]
        if not os.path.exists(path):
            raise ConfigError(f"config file not found: {path}")
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        if not isinstance(cfg, dict):
            raise ConfigError(f"config root must be a mapping: {path}")
        cfg["_path"] = path
        _cache[path] = cfg
        return cfg


def reload_config(path=None):
    path = os.path.abspath(path or default_config_path())
    with _lock:
        _cache.pop(path, None)
    return load_config(path)


def get(cfg, dotted, default=None):
    node = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def _empty(v):
    return v is None or (isinstance(v, str) and v.strip() == "")


def validate_config(cfg):
    missing = [k for k in REQUIRED_KEYS if _empty(get(cfg, k))]
    if missing:
        raise ConfigError("missing or empty required config keys: " + ", ".join(missing))
    return True


def save_config(cfg, path=None):
    """Round trip write that preserves comments. Only keys present in cfg are updated."""
    path = os.path.abspath(path or cfg.get("_path") or default_config_path())
    ry = YAML()
    ry.preserve_quotes = True
    ry.indent(mapping=2, sequence=4, offset=2)
    with open(path, encoding="utf-8") as f:
        doc = ry.load(f) or {}
    _merge(doc, {k: v for k, v in cfg.items() if k != "_path"})
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        ry.dump(doc, f)
    os.replace(tmp, path)
    with _lock:
        _cache.pop(path, None)


def _merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = v
