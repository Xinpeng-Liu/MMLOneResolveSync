"""Port of services/clip-composer/export/nle/sync-diff.ts.

Identity = element id. Equality fields are a fixed allow-list — mediaUrl is
NOT in it because Convex signed URLs rotate every read.
"""
from typing import Any, Dict, List, Tuple

CLIP_EQ_FIELDS = (
    'assetKey', 'trackId', 'startTime', 'duration',
    'trimStart', 'trimEnd', 'speed', 'volume',
)
OVERLAY_EQ_FIELDS = (
    'kind', 'assetKey', 'startTime', 'duration',
    'x', 'y', 'scaleX', 'scaleY', 'opacity', 'rotation', 'volume',
)


def _diff_one(category: str, prev: List[Dict], nxt: List[Dict], eq_fields: Tuple[str, ...]):
    by_prev = {p['id']: p for p in prev}
    by_next = {n['id']: n for n in nxt}
    added: List[Dict] = []
    modified: List[Dict] = []
    removed: List[Dict] = []
    unchanged = 0

    for n in nxt:
        p = by_prev.get(n['id'])
        if p is None:
            added.append({'category': category, 'id': n['id'], 'after': n})
            continue
        field_changes = [f for f in eq_fields if p.get(f) != n.get(f)]
        if not field_changes:
            unchanged += 1
        else:
            modified.append({
                'category': category, 'id': n['id'],
                'before': p, 'after': n, 'fieldChanges': field_changes,
            })
    for p in prev:
        if p['id'] not in by_next:
            removed.append({'category': category, 'id': p['id'], 'before': p})
    return added, modified, removed, unchanged


def diff_sync_state(prev: Dict[str, Any], nxt: Dict[str, Any]) -> Dict[str, Any]:
    va, vm, vr, vu = _diff_one('video', prev.get('clips', []), nxt.get('clips', []), CLIP_EQ_FIELDS)
    ia, im, ir, iu = _diff_one(
        'image', prev.get('imageOverlays', []), nxt.get('imageOverlays', []), OVERLAY_EQ_FIELDS,
    )
    aa, am, ar, au = _diff_one(
        'audio', prev.get('audioClips', []), nxt.get('audioClips', []), OVERLAY_EQ_FIELDS,
    )
    return {
        'added': va + ia + aa,
        'modified': vm + im + am,
        'removed': vr + ir + ar,
        'unchanged': vu + iu + au,
    }
