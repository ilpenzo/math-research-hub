"""Optional local Ollama embeddings, never remote inference or model downloads."""
from __future__ import annotations

import ipaddress
import json
import math
from pathlib import Path
import time
from urllib.parse import urlparse
import urllib.error
import urllib.request

QUERY_TIMEOUT = 1.5
MAX_INPUT_CHARS = 12000


class ProviderUnavailable(ValueError):
    pass


def _local_url(url):
    parsed = urlparse(url)
    if parsed.scheme != 'http' or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Embedding URL must be an uncredentialed local HTTP endpoint')
    host = parsed.hostname
    if host != 'localhost':
        try:
            local = ipaddress.ip_address(host or '').is_loopback
        except ValueError:
            local = False
        if not local:
            raise ValueError('Embedding provider must be localhost or a loopback IP')
    if parsed.path.rstrip('/'):
        raise ValueError('Embedding URL must name the local server root')
    return url.rstrip('/')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ProviderUnavailable('Local embedding redirects are refused')


class Ollama:
    def __init__(self, config):
        self.url = _local_url(config.get('url', 'http://127.0.0.1:11434'))
        self.model = config.get('model', '')
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError('Embedding provider needs an explicitly configured local model')
        self.expected_digest = config.get('digest')
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def _request(self, route, payload=None, timeout=QUERY_TIMEOUT):
        request = urllib.request.Request(self.url + route,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=max(.05, timeout)) as response:
                return json.load(response)
        except (OSError, ValueError, urllib.error.URLError) as exc:
            raise ProviderUnavailable(f'Local Ollama unavailable: {exc}') from exc

    def identity(self, timeout=QUERY_TIMEOUT):
        data = self._request('/api/tags', timeout=timeout)
        choices = {self.model, self.model + ':latest'}
        match = next((m for m in data.get('models', [])
                      if m.get('name') in choices or m.get('model') in choices), None)
        if not match or not match.get('digest'):
            raise ProviderUnavailable(f'Configured model {self.model} is not installed with an artifact digest')
        if self.expected_digest and self.expected_digest != match['digest']:
            raise ProviderUnavailable('Configured model artifact digest changed')
        return dict(provider='ollama', model=match.get('name', self.model), digest=match['digest'],
                    url=self.url, input_policy=f'first {MAX_INPUT_CHARS} characters; Ollama truncate=true',
                    details=match.get('details', {}))

    def embed(self, texts, timeout=60):
        data = self._request('/api/embed', dict(model=self.model,
            input=[text[:MAX_INPUT_CHARS] for text in texts], truncate=True), timeout=timeout)
        vectors = data.get('embeddings')
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise ProviderUnavailable('Ollama returned an invalid embedding batch')
        size = None
        for vector in vectors:
            if not isinstance(vector, list) or not vector or any(
                not isinstance(x, (int, float)) or not math.isfinite(x) for x in vector):
                raise ProviderUnavailable('Ollama returned invalid embedding values')
            size = size or len(vector)
            if len(vector) != size:
                raise ProviderUnavailable('Ollama changed embedding dimensions within a batch')
        return vectors


def configured(hub):
    path = Path(hub) / 'catalog/recall-config.json'
    if not path.exists():
        return None
    try:
        config = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ProviderUnavailable(f'Cannot read local embedding configuration: {exc}') from exc
    if config.get('enabled', True) is False:
        return None
    if config.get('provider', 'ollama') != 'ollama':
        raise ProviderUnavailable('Only the local Ollama embedding adapter is supported')
    return Ollama(config)


def query_vector(hub, text, pin, timeout=QUERY_TIMEOUT):
    """Bound the entire online query budget, including model identity verification."""
    started = time.monotonic()
    provider = configured(hub)
    if provider is None:
        raise ProviderUnavailable('No local embedding provider configured')
    current = provider.identity(timeout=timeout)
    if any(current.get(k) != pin.get(k) for k in ('provider', 'model', 'digest')):
        raise ProviderUnavailable('Index model artifact differs from installed model; refresh required')
    remaining = timeout - (time.monotonic() - started)
    if remaining <= .05:
        raise ProviderUnavailable('Local embedding query time budget exhausted')
    vector = provider.embed([text], timeout=remaining)[0]
    if len(vector) != pin.get('dimensions'):
        raise ProviderUnavailable('Embedding dimensions differ from pinned index')
    return vector
