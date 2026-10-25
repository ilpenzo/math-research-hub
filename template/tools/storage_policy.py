"""Small configurable storage budgets; no pruning of canonical/history data."""
import json
from pathlib import Path
import shutil

CONFIG = 'catalog/storage-config.json'
DEFAULTS = dict(schema_version=1, free_reserve_bytes=5 * 1024**3,
    generation_budget_bytes=1024**3, cache_budget_bytes=1024**3,
    catalog_warning_bytes=20 * 1024**2, transaction_warning_bytes=50 * 1024**2,
    journal_warning_bytes=2 * 1024**3, daily_growth_warning_bytes=250 * 1024**2)


def validate(value):
    if not isinstance(value, dict) or value.get('schema_version') != 1:
        raise ValueError('Storage configuration requires schema_version 1')
    if set(value) - set(DEFAULTS):
        raise ValueError('Unknown storage configuration field')
    for key, number in value.items():
        if type(number) is not int or number < (0 if key == 'free_reserve_bytes' else 1):
            raise ValueError('Storage budgets must be positive integers (reserve may be zero)')
    return dict(DEFAULTS, **value)


def load(hub, overrides=None):
    path = Path(hub) / CONFIG
    if overrides is not None and CONFIG in overrides:
        raw = overrides[CONFIG]
    else:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            raw = None
    return dict(DEFAULTS) if raw is None else validate(json.loads(raw))


def require_space(hub, needed, *, reserve=None, overrides=None):
    reserve = load(hub, overrides)['free_reserve_bytes'] if reserve is None else reserve
    free = shutil.disk_usage(hub).free
    if free < needed + reserve:
        raise ValueError(f'Insufficient free disk space: {free} bytes available; '
                         f'{needed} staging/recovery bytes + {reserve} reserve required')
    return free
