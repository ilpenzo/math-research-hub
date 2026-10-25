"""Lossless, versioned payload compression (Python 3.11+, no required dependency)."""
from contextlib import contextmanager
import hashlib
import io
import lzma

try:
    from compression import zstd as _zstd
except ImportError:
    _zstd = None
try:
    import zstandard as _zstandard
except ImportError:
    _zstandard = None

EXTENSIONS = {'zstd': 'zst', 'xz': 'xz'}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def preferred_codec():
    return 'zstd' if _zstd is not None or _zstandard is not None else 'xz'


def compress(raw, codec=None):
    codec = codec or preferred_codec()
    if codec == 'zstd':
        if _zstd is not None:
            return _zstd.compress(raw, level=3)
        if _zstandard is not None:
            return _zstandard.ZstdCompressor(level=3).compress(raw)
        raise ValueError('Reading/writing zstd requires Python 3.14+ or the optional zstandard package')
    if codec == 'xz':
        return lzma.compress(raw, preset=3)
    raise ValueError('Unsupported storage codec')


@contextmanager
def reader(packed, codec):
    source = io.BytesIO(packed)
    if codec == 'xz':
        stream = lzma.LZMAFile(source, 'rb')
    elif codec == 'zstd' and _zstd is not None:
        stream = _zstd.ZstdFile(source, 'rb')
    elif codec == 'zstd' and _zstandard is not None:
        stream = _zstandard.ZstdDecompressor().stream_reader(source)
    else:
        raise ValueError('Unsupported/unavailable storage codec; install zstandard to read zstd on Python <3.14')
    with stream:
        yield stream


def reference(raw, packed, codec):
    return dict(codec=codec, sha256=digest(raw), bytes=len(raw),
                stored_sha256=digest(packed), stored_bytes=len(packed))


def validate_reference(ref):
    import re
    if not isinstance(ref, dict) or set(ref) != {'codec', 'sha256', 'bytes', 'stored_sha256', 'stored_bytes'}:
        raise ValueError('Invalid storage reference fields')
    if ref['codec'] not in EXTENSIONS:
        raise ValueError('Unsupported storage codec')
    for key in ('sha256', 'stored_sha256'):
        if not isinstance(ref[key], str) or not re.fullmatch('[a-f0-9]{64}', ref[key]):
            raise ValueError('Invalid storage digest')
    for key in ('bytes', 'stored_bytes'):
        if type(ref[key]) is not int or ref[key] < 0:
            raise ValueError('Invalid storage length')


def unpack(packed, ref):
    validate_reference(ref)
    if len(packed) != ref['stored_bytes'] or digest(packed) != ref['stored_sha256']:
        raise ValueError('Compressed payload checksum/length mismatch')
    try:
        with reader(packed, ref['codec']) as stream:
            raw = stream.read(ref['bytes'] + 1)
            if len(raw) != ref['bytes'] or stream.read(1):
                raise ValueError('Decoded payload length mismatch')
    except (EOFError, lzma.LZMAError) as exc:
        raise ValueError('Corrupt compressed payload') from exc
    if digest(raw) != ref['sha256']:
        raise ValueError('Decoded payload checksum mismatch')
    return raw


def pack(raw, codec=None):
    codec = codec or preferred_codec()
    packed = compress(raw, codec)
    ref = reference(raw, packed, codec)
    # Verify the representation before it can replace any original bytes.
    if unpack(packed, ref) != raw:
        raise ValueError('Compression round trip failed')
    return ref, packed


def filename(ref):
    validate_reference(ref)
    return ref['sha256'] + '.' + EXTENSIONS[ref['codec']]
