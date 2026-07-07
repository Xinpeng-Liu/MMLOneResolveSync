"""Media download helpers: atomic size-bounded fetch, cache reuse, prefetch.

Extracted from ui.py so the Preview-Sync apply and Import-Media flows share one
downloader. Cache validity = exact byte-size match against the wire-declared
`fileSize`; items without a declared size always re-download (a stale cache
file is indistinguishable from a current one).
"""
import os
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .resolve_apply import _ext_for

# Prefetch result statuses — callers count these for the summary log line.
CACHED = 'cached'
FETCHED = 'fetched'
FAILED = 'failed'


def download(url: str, dest: Path, *, max_bytes: int, timeout: float) -> Path:
    """Atomic + size-bounded download. Writes to <dest>.tmp, verifies size,
    then os.replace to <dest>. A network drop or oversized response leaves
    no partial file at the cache path Resolve will read from."""
    tmp = dest.with_suffix(dest.suffix + '.tmp')
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            declared = int(r.headers.get('Content-Length') or 0)
            if declared and declared > max_bytes:
                raise IOError(f'media too large: {declared} > {max_bytes}')
            written = 0
            with open(tmp, 'wb') as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        raise IOError(f'media exceeded {max_bytes} bytes mid-stream')
                    f.write(chunk)
            if declared and written != declared:
                raise IOError(f'short download: {written}/{declared}')
        os.replace(tmp, dest)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return dest


def is_cached(dest: Path, expected_size: Optional[int]) -> bool:
    if not isinstance(expected_size, int) or expected_size <= 0:
        return False
    try:
        return dest.is_file() and dest.stat().st_size == expected_size
    except OSError:
        return False


def cached_or_download(url: str, dest: Path, *, expected_size: Optional[int],
                       max_bytes: int, timeout: float) -> Path:
    if is_cached(dest, expected_size):
        return dest
    return download(url, dest, max_bytes=max_bytes, timeout=timeout)


def media_jobs(items: Iterable[Dict[str, Any]],
               cache: Path) -> List[Tuple[str, Path, Optional[int]]]:
    """(url, dest, expected_size) per distinct assetKey. Mirrors the cache-path
    scheme in resolve_apply so the prefetched file is the exact path
    apply_diff / import_media_only will read."""
    jobs: List[Tuple[str, Path, Optional[int]]] = []
    seen: set = set()
    for item in items:
        key, url = item.get('assetKey'), item.get('mediaUrl')
        if not key or not url or key in seen:
            continue
        seen.add(key)
        dest = cache / f'{key.replace(":", "_")}.{_ext_for(item)}'
        size = item.get('fileSize')
        jobs.append((url, dest, size if isinstance(size, int) and size > 0 else None))
    return jobs


def prefetch(jobs: Iterable[Tuple[str, Path, Optional[int]]], *,
             max_bytes: int, timeout: float,
             workers: int = 4) -> Dict[Path, Tuple[str, Optional[Exception]]]:
    """Parallel cached_or_download over (url, dest, expected_size) jobs.
    Returns {dest: (status, exception-or-None)}. Never raises — apply_diff /
    import_media_only retry per item, so a prefetch failure only costs the
    parallelism, not the sync."""
    # Dedupe by dest: two workers streaming into the same <dest>.tmp would
    # corrupt each other.
    unique: Dict[Path, Tuple[str, Optional[int]]] = {}
    for url, dest, expected_size in jobs:
        unique.setdefault(Path(dest), (url, expected_size))
    results: Dict[Path, Tuple[str, Optional[Exception]]] = {}
    if not unique:
        return results

    def fetch_one(url: str, dest: Path,
                  expected_size: Optional[int]) -> Tuple[str, Optional[Exception]]:
        if is_cached(dest, expected_size):
            return (CACHED, None)
        try:
            download(url, dest, max_bytes=max_bytes, timeout=timeout)
            return (FETCHED, None)
        except Exception as e:
            return (FAILED, e)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {dest: pool.submit(fetch_one, url, dest, size)
                   for dest, (url, size) in unique.items()}
    # `with` joins the pool, so every future is done here.
    for dest, fut in futures.items():
        results[dest] = fut.result()
    return results


def summary_line(results: Dict[Path, Tuple[str, Optional[Exception]]]) -> str:
    counts = {CACHED: 0, FETCHED: 0, FAILED: 0}
    for status, _err in results.values():
        counts[status] = counts.get(status, 0) + 1
    return (f'{counts[FETCHED]} fetched, {counts[CACHED]} cached, '
            f'{counts[FAILED]} failed')
