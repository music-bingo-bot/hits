import yaml
from functools import lru_cache

@lru_cache(maxsize=1)
def _load():
    with open("messages.yaml", "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def get(key: str, **kwargs) -> str:
    data = _load()
    val = data.get(key, key)
    try:
        return val.format(**kwargs)
    except Exception:
        return val
