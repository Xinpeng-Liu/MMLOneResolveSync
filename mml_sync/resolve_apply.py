"""Translates a SyncDiff into Resolve API calls. Fakeable via duck typing.

`resolve` is anything that implements `GetProjectManager()` returning an object
with `GetCurrentProject()`. The fake in tests is duck-compatible with the real
`DaVinciResolveScript` `Resolve` instance.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List

from . import config, storage


class FrameRateMismatch(Exception):
    pass


class TimelineNotFound(Exception):
    pass


@dataclass
class ApplyResult:
    added: int = 0
    modified: int = 0
    removed: int = 0
    skipped: int = 0
    warnings: List[str] = field(default_factory=list)


_MEDIA_TYPE_VIDEO = 1
_MEDIA_TYPE_AUDIO = 2

_EXT_BY_MIME = {
    'video/mp4': 'mp4', 'video/quicktime': 'mov', 'video/webm': 'webm',
    'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp',
    'audio/mpeg': 'mp3', 'audio/wav': 'wav', 'audio/mp4': 'm4a',
}

# Magic-byte signatures for the formats Resolve cares about. Resolve refuses
# to load a file when the extension lies about the content (the upstream
# pipeline sometimes labels JPEGs as `.png`), so we re-detect the actual
# format from the first few bytes and rename if needed.
_MAGIC_SIGNATURES = (
    (b'\x89PNG\r\n\x1a\n', 'png'),
    (b'\xff\xd8\xff', 'jpg'),
    (b'RIFF', 'webp'),  # WEBP also starts with RIFF; refined below if needed
    (b'GIF87a', 'gif'),
    (b'GIF89a', 'gif'),
    (b'ID3', 'mp3'),
    (b'\xff\xfb', 'mp3'),
    (b'\xff\xf3', 'mp3'),
    (b'\xff\xf2', 'mp3'),
    (b'OggS', 'ogg'),
    (b'fLaC', 'flac'),
)


def _sniff_extension(path: Path) -> str:
    """Read the first 16 bytes of `path` and return a known extension if a
    magic-byte signature matches, else ''. Cheap, no third-party deps."""
    try:
        with open(path, 'rb') as f:
            head = f.read(16)
    except OSError:
        return ''
    for sig, ext in _MAGIC_SIGNATURES:
        if head.startswith(sig):
            # WEBP refinement: 'RIFF????WEBP'
            if ext == 'webp' and len(head) >= 12 and head[8:12] != b'WEBP':
                continue
            return ext
    # MP4/MOV/QuickTime: 'ftyp' brand at offset 4
    if len(head) >= 12 and head[4:8] == b'ftyp':
        brand = head[8:12]
        if brand in (b'qt  ',):
            return 'mov'
        return 'mp4'
    return ''


def _normalize_extension(local_path: Path) -> Path:
    """If the file at `local_path` has a magic-byte format that disagrees with
    its current extension, rename it to use the actual format's extension and
    return the new path. Otherwise return `local_path` unchanged."""
    actual = _sniff_extension(local_path)
    if not actual:
        return local_path
    current = local_path.suffix.lstrip('.').lower()
    canonical = {'jpeg': 'jpg'}.get(current, current)
    if canonical == actual:
        return local_path
    new_path = local_path.with_suffix('.' + actual)
    try:
        # Best-effort: if a stale file exists at new_path, replace it.
        if new_path.exists():
            new_path.unlink()
        local_path.rename(new_path)
        return new_path
    except OSError:
        return local_path


def _seconds_to_frames(seconds: float, fps: float) -> int:
    return int(round(seconds * fps))


def _ext_for(clip: Dict[str, Any]) -> str:
    name = (clip.get('mediaName') or '').lower()
    if '.' in name:
        return name.rsplit('.', 1)[-1]
    return _EXT_BY_MIME.get(clip.get('mediaMimeType') or '', 'bin')


def _ensure_timeline(project, timeline_name: str):
    """Look up a timeline by name (Resolve has no GetTimelineByName, so we walk
    the index). Create it via MediaPool.CreateEmptyTimeline if absent.

    On creation we also pre-allocate a second video track for image overlays
    (which land on V2). Resolve's AppendToTimeline does NOT auto-create
    missing tracks, so without this every image overlay would fail.
    """
    count = int(project.GetTimelineCount() or 0)
    for i in range(1, count + 1):
        tl = project.GetTimelineByIndex(i)
        if tl is not None and tl.GetName() == timeline_name:
            project.SetCurrentTimeline(tl)
            _ensure_minimum_tracks(tl)
            return tl
    tl = project.GetMediaPool().CreateEmptyTimeline(timeline_name)
    if tl is None:
        raise TimelineNotFound(f'Resolve refused to create timeline {timeline_name!r}')
    project.SetCurrentTimeline(tl)
    _ensure_minimum_tracks(tl)
    return tl


def _ensure_minimum_tracks(tl) -> None:
    """Make sure the timeline has at least V2 (image overlays) and A1 (audio
    overlays). Resolve creates new timelines with V1 + A1 by default, so we
    only need to add V2."""
    try:
        video_count = int(tl.GetTrackCount('video') or 0)
    except (AttributeError, TypeError):
        video_count = 0
    while video_count < 2:
        added = tl.AddTrack('video') if hasattr(tl, 'AddTrack') else False
        if not added:
            break
        video_count += 1


def _ensure_media(media_pool, asset_key: str, mapping: Dict[str, Any], local_path: Path):
    """Import media into Resolve once per asset_key. Returns MediaPoolItem-like."""
    items = media_pool.ImportMedia([str(local_path)])
    if not items:
        return None
    item = items[0]
    mapping['mediaKeyToMediaPoolItemId'][asset_key] = item.GetUniqueId()
    return item


def _append_clip(media_pool, *, media, source_start_frame: int, source_end_frame: int,
                 record_frame: int, track_index: int, media_type=None):
    """Place `media` on the current timeline using the documented Resolve clipInfo
    schema: `startFrame`/`endFrame` are SOURCE in/out, `recordFrame` is the
    timeline placement.

    `media_type` semantics:
      None  → place video AND audio (Resolve mirrors the manual drag-from-pool
              behaviour: video on the requested trackIndex, audio auto-routed
              to A1). Use for SyncClip (full A/V clip).
      1     → video only on trackIndex (used for image overlays on V2).
      2     → audio only on trackIndex (used for standalone audio overlays).
    Returns the new TimelineItem or None.
    """
    info: Dict[str, Any] = {
        'mediaPoolItem': media,
        'startFrame': source_start_frame,
        'endFrame': source_end_frame,
        'recordFrame': record_frame,
        'trackIndex': track_index,
    }
    if media_type is not None:
        info['mediaType'] = media_type
    items = media_pool.AppendToTimeline([info])
    return items[0] if items else None


def _timeline_start_frame(tl) -> int:
    """Resolve timelines have a non-zero start frame (default 01:00:00:00,
    which is fps * 3600). AppendToTimeline silently drops clips whose
    `recordFrame` falls before this offset, so we always add the start frame
    to MML ONE's seconds-based positions."""
    try:
        if hasattr(tl, 'GetStartFrame'):
            return int(tl.GetStartFrame() or 0)
    except (AttributeError, TypeError, ValueError):
        pass
    return 0


def _append_clip_for_clip(media_pool, tl, *, media, clip: Dict[str, Any], fps: float,
                          track_index: int, media_type=None):
    """Translate a SyncClip's seconds-based timing into Resolve frame fields.
    `media_type=None` lets Resolve place both video AND audio (matches the
    manual drag-from-pool behaviour). Callers should leave it None for full
    A/V clips."""
    source_start = _seconds_to_frames(clip['trimStart'], fps)
    source_end = _seconds_to_frames(clip['trimStart'] + clip['duration'], fps)
    record_frame = _seconds_to_frames(clip['startTime'], fps) + _timeline_start_frame(tl)
    return _append_clip(media_pool, media=media,
                        source_start_frame=source_start,
                        source_end_frame=source_end,
                        record_frame=record_frame,
                        track_index=track_index, media_type=media_type)


def _append_overlay(media_pool, tl, *, media, overlay: Dict[str, Any], fps: float,
                    track_index: int, media_type: int):
    """SyncOverlay uses absolute timeline timing only — no trim metadata,
    so source = full [0, duration]."""
    source_end = _seconds_to_frames(overlay['duration'], fps)
    record_frame = _seconds_to_frames(overlay['startTime'], fps) + _timeline_start_frame(tl)
    return _append_clip(media_pool, media=media,
                        source_start_frame=0,
                        source_end_frame=source_end,
                        record_frame=record_frame,
                        track_index=track_index, media_type=media_type)


def _find_timeline_item(tl, unique_id: str):
    """Scan every track on the timeline for a TimelineItem with the given id."""
    for kind in ('video', 'audio'):
        try:
            count = int(tl.GetTrackCount(kind))
        except (AttributeError, TypeError):
            count = 16
        for idx in range(1, max(count, 1) + 1):
            for it in tl.GetItemListInTrack(kind, idx):
                if it.GetUniqueId() == unique_id:
                    return it
    return None


def import_media_only(*, resolve: Any, root: Path, project_id: str,
                      episode_id: str, current_state: Dict[str, Any],
                      downloader: Callable[[str, Path], Path]) -> ApplyResult:
    """Download every distinct media asset referenced by `current_state` and
    import it into Resolve's Media Pool — but do NOT touch the timeline.

    Used when the editor wants to assemble the timeline themselves in Resolve
    while still pulling MML ONE-generated assets into the project. Snapshot
    is intentionally NOT advanced; the mapping IS updated so a later
    Preview/Force Sync re-uses the same MediaPoolItems instead of duplicating.
    """
    project = resolve.GetProjectManager().GetCurrentProject()
    if project is None:
        raise TimelineNotFound('No project open in Resolve')
    media_pool = project.GetMediaPool()

    mapping = storage.load_mapping(root, project_id, episode_id)
    cache = config.media_cache_dir(root, project_id)
    cache.mkdir(parents=True, exist_ok=True)
    result = ApplyResult()

    # Dedupe by assetKey across clips + overlays.
    seen: set = set()
    items: List[Dict[str, Any]] = []
    for bucket in ('clips', 'imageOverlays', 'audioClips'):
        for c in (current_state.get(bucket) or []):
            key = c.get('assetKey')
            if not key or key in seen:
                continue
            seen.add(key)
            items.append(c)

    for item in items:
        local = cache / f'{item["assetKey"].replace(":", "_")}.{_ext_for(item)}'
        try:
            downloader(item['mediaUrl'], local)
        except Exception as e:
            result.warnings.append(f'Download failed for {item["assetKey"]}: {e}')
            result.skipped += 1
            continue
        local = _normalize_extension(local)
        media = _ensure_media(media_pool, item['assetKey'], mapping, local)
        if media is None:
            result.warnings.append(f'Could not import media for {item["assetKey"]}')
            result.skipped += 1
            continue
        result.added += 1

    storage.save_mapping(root, project_id, episode_id, mapping)
    return result


def reconcile_drift(*, resolve: Any, root: Path, project_id: str,
                    episode_id: str, timeline_name: str,
                    snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Detect manual deletions in Resolve and rewrite the snapshot to reflect
    them, so the next diff treats those clips as added (re-imports them).

    Walks `mapping.clipIdToTimelineItemId`, asks Resolve whether each tracked
    TimelineItem still exists, and for any that vanished:
      - drops the entry from mapping
      - drops the clip/overlay with that id from the supplied snapshot copy
    Returns the (possibly mutated) snapshot. Caller owns persistence.
    """
    project = resolve.GetProjectManager().GetCurrentProject()
    if project is None:
        return snapshot
    # Find the timeline by name; if absent, every tracked item is gone.
    tl = None
    count = int(project.GetTimelineCount() or 0)
    for i in range(1, count + 1):
        candidate = project.GetTimelineByIndex(i)
        if candidate is not None and candidate.GetName() == timeline_name:
            tl = candidate
            break

    mapping = storage.load_mapping(root, project_id, episode_id)
    tracked: Dict[str, str] = dict(mapping.get('clipIdToTimelineItemId') or {})
    if not tracked:
        return snapshot

    missing_ids = set()
    for clip_id, ti_id in tracked.items():
        if tl is None:
            missing_ids.add(clip_id)
            continue
        if _find_timeline_item(tl, ti_id) is None:
            missing_ids.add(clip_id)

    if not missing_ids:
        return snapshot

    # Drop missing entries from mapping.
    for cid in missing_ids:
        mapping['clipIdToTimelineItemId'].pop(cid, None)
    # If the timeline itself is gone, also forget the media-pool mapping —
    # those items were almost certainly deleted alongside the timeline, and
    # keeping stale ids would prevent re-import.
    if tl is None:
        mapping['mediaKeyToMediaPoolItemId'] = {}
    storage.save_mapping(root, project_id, episode_id, mapping)

    # Drop the same ids from the snapshot so diff treats them as added.
    snap = dict(snapshot)
    for field in ('clips', 'imageOverlays', 'audioClips'):
        snap[field] = [c for c in (snap.get(field) or []) if c.get('id') not in missing_ids]
    return snap


def reset_resolve_state(*, resolve: Any, root: Path, project_id: str,
                        episode_id: str, timeline_name: str) -> Dict[str, Any]:
    """Force-sync helper: removes the named timeline and the MediaPoolItems
    that mml_sync previously imported for this episode, so the next apply_diff
    call lands as a full re-add. The local snapshot/mapping/media cache are
    cleared by the caller (UI layer)."""
    project = resolve.GetProjectManager().GetCurrentProject()
    if project is None:
        return {'timelinesDeleted': 0, 'mediaItemsDeleted': 0}

    # 1. Delete the target timeline if it exists. Resolve has no
    #    Project.DeleteTimelines API everywhere, but MediaPool.DeleteTimelines
    #    is documented and works on free + Studio.
    media_pool = project.GetMediaPool()
    timelines_deleted = 0
    count = int(project.GetTimelineCount() or 0)
    for i in range(1, count + 1):
        tl = project.GetTimelineByIndex(i)
        if tl is None:
            continue
        if tl.GetName() == timeline_name:
            try:
                if hasattr(media_pool, 'DeleteTimelines'):
                    media_pool.DeleteTimelines([tl])
                    timelines_deleted += 1
            except Exception as e:
                print(f'[mml_sync] timeline delete failed: {e}')
            break

    # 2. Delete media-pool items previously imported for this episode.
    #    The mapping is the only authoritative record we have; Resolve's
    #    MediaPool.DeleteClips wants MediaPoolItem objects which we need to
    #    look up by id, since the API returns raw mediaPoolItems for a
    #    folder via GetClipList().
    mapping = storage.load_mapping(root, project_id, episode_id)
    target_ids = set((mapping.get('mediaKeyToMediaPoolItemId') or {}).values())
    media_deleted = 0
    if target_ids and hasattr(media_pool, 'GetRootFolder'):
        try:
            root_folder = media_pool.GetRootFolder()
            to_delete = []
            stack = [root_folder]
            while stack:
                folder = stack.pop()
                if folder is None:
                    continue
                if hasattr(folder, 'GetClipList'):
                    for clip in (folder.GetClipList() or []):
                        try:
                            if clip.GetUniqueId() in target_ids:
                                to_delete.append(clip)
                        except (AttributeError, TypeError):
                            pass
                if hasattr(folder, 'GetSubFolderList'):
                    stack.extend(folder.GetSubFolderList() or [])
            if to_delete and hasattr(media_pool, 'DeleteClips'):
                media_pool.DeleteClips(to_delete)
                media_deleted = len(to_delete)
        except Exception as e:
            print(f'[mml_sync] media delete failed: {e}')

    return {'timelinesDeleted': timelines_deleted, 'mediaItemsDeleted': media_deleted}


def apply_diff(*,
               resolve: Any,
               root: Path,
               project_id: str,
               episode_id: str,
               timeline_name: str,
               current_state: Dict[str, Any],
               diff: Dict[str, Any],
               downloader: Callable[[str, Path], Path]) -> ApplyResult:
    """Apply the diff against the current Resolve project. `downloader(url, dest)`
    is injected so tests can avoid real HTTP and the live plugin can pass a
    streaming download helper."""
    project = resolve.GetProjectManager().GetCurrentProject()
    if project is None:
        raise TimelineNotFound('No project open in Resolve')

    expected_fps = float(current_state['fps'])
    project_fps = float(project.GetSetting('timelineFrameRate') or expected_fps)
    if abs(project_fps - expected_fps) > 0.01:
        raise FrameRateMismatch(
            f'Resolve project is {project_fps} fps; MML ONE timeline is {expected_fps} fps. '
            'Open a project that matches, or change the project fps.'
        )

    tl = _ensure_timeline(project, timeline_name)
    media_pool = project.GetMediaPool()

    mapping = storage.load_mapping(root, project_id, episode_id)
    mapping['resolveProjectName'] = project.GetName()
    mapping['resolveTimelineName'] = timeline_name

    cache = config.media_cache_dir(root, project_id)
    cache.mkdir(parents=True, exist_ok=True)
    result = ApplyResult()

    # ---- added ----
    for change in diff['added']:
        category = change['category']
        clip = change['after']
        local = cache / f'{clip["assetKey"].replace(":", "_")}.{_ext_for(clip)}'
        try:
            downloader(clip['mediaUrl'], local)
        except Exception as e:
            result.warnings.append(f'Download failed for {clip["id"]}: {e}')
            result.skipped += 1
            storage.save_mapping(root, project_id, episode_id, mapping)
            continue
        local = _normalize_extension(local)
        media = _ensure_media(media_pool, clip['assetKey'], mapping, local)
        if media is None:
            result.warnings.append(f'Could not import media for {clip["id"]}')
            result.skipped += 1
            storage.save_mapping(root, project_id, episode_id, mapping)
            continue
        if category == 'video':
            # No mediaType → Resolve places video + audio together (V1 + A1),
            # matching the user's manual drag-from-pool expectation.
            item = _append_clip_for_clip(media_pool, tl, media=media, clip=clip,
                                         fps=expected_fps, track_index=1)
        elif category == 'image':
            # Image overlays land on V2 above the main video so they composite on top.
            # Per-image transform (x/y/scale/rotation/opacity) cannot be set via the
            # scripting API on free or Studio — the editor adjusts in Inspector.
            item = _append_overlay(media_pool, tl, media=media, overlay=clip,
                                   fps=expected_fps,
                                   track_index=2, media_type=_MEDIA_TYPE_VIDEO)
        elif category == 'audio':
            item = _append_overlay(media_pool, tl, media=media, overlay=clip,
                                   fps=expected_fps,
                                   track_index=1, media_type=_MEDIA_TYPE_AUDIO)
        else:
            storage.save_mapping(root, project_id, episode_id, mapping)
            continue
        if item is None:
            result.warnings.append(f'Resolve refused to add {clip["id"]} to the timeline')
            result.skipped += 1
            storage.save_mapping(root, project_id, episode_id, mapping)
            continue
        mapping['clipIdToTimelineItemId'][clip['id']] = item.GetUniqueId()
        result.added += 1
        storage.save_mapping(root, project_id, episode_id, mapping)

    # ---- modified ----
    # Order: download → import → APPEND new → only THEN delete old. The new item
    # lands BEFORE the old goes away so an append failure leaves the prior clip intact.
    for change in diff['modified']:
        category = change['category']
        clip = change['after']
        ti_id = mapping['clipIdToTimelineItemId'].get(change['id'])

        local = cache / f'{clip["assetKey"].replace(":", "_")}.{_ext_for(clip)}'
        try:
            downloader(clip['mediaUrl'], local)
        except Exception as e:
            result.warnings.append(
                f'Download failed for {change["id"]}: {e}; keeping previous clip'
            )
            result.skipped += 1
            storage.save_mapping(root, project_id, episode_id, mapping)
            continue
        local = _normalize_extension(local)
        media = _ensure_media(media_pool, clip['assetKey'], mapping, local)
        if media is None:
            result.warnings.append(
                f'Could not import media for {change["id"]}; keeping previous clip'
            )
            result.skipped += 1
            storage.save_mapping(root, project_id, episode_id, mapping)
            continue

        if category == 'video':
            new_item = _append_clip_for_clip(media_pool, tl, media=media, clip=clip,
                                             fps=expected_fps, track_index=1)
        elif category == 'image':
            new_item = _append_overlay(media_pool, tl, media=media, overlay=clip,
                                       fps=expected_fps,
                                       track_index=2, media_type=_MEDIA_TYPE_VIDEO)
        elif category == 'audio':
            new_item = _append_overlay(media_pool, tl, media=media, overlay=clip,
                                       fps=expected_fps,
                                       track_index=1, media_type=_MEDIA_TYPE_AUDIO)
        else:
            storage.save_mapping(root, project_id, episode_id, mapping)
            continue

        if new_item is None:
            result.warnings.append(
                f'Resolve refused to re-add {change["id"]}; previous clip preserved'
            )
            result.skipped += 1
            storage.save_mapping(root, project_id, episode_id, mapping)
            continue

        if ti_id:
            existing = _find_timeline_item(tl, ti_id)
            if existing is not None and existing.GetUniqueId() != new_item.GetUniqueId():
                tl.DeleteClips([existing])

        new_item.SetClipColor('Yellow')
        new_item.AddMarker(
            frame=new_item.GetStart(),
            color='Yellow',
            name='MML ONE: re-applied',
            note='fields: ' + ', '.join(change.get('fieldChanges') or []),
            duration=1,
            custom_data='mml_one_modified',
        )
        mapping['clipIdToTimelineItemId'][change['id']] = new_item.GetUniqueId()
        result.modified += 1
        storage.save_mapping(root, project_id, episode_id, mapping)

    # ---- removed (soft-delete: red marker, leave the clip on the timeline) ----
    for change in diff['removed']:
        ti_id = mapping['clipIdToTimelineItemId'].get(change['id'])
        if not ti_id:
            continue
        target = _find_timeline_item(tl, ti_id)
        if target is None:
            mapping['clipIdToTimelineItemId'].pop(change['id'], None)
            storage.save_mapping(root, project_id, episode_id, mapping)
            continue
        target.SetClipColor('Red')
        target.AddMarker(
            frame=target.GetStart(), color='Red',
            name='MML ONE: removed upstream',
            note='Removed in MML ONE — kept here for editing.',
            duration=1, custom_data='mml_one_removed',
        )
        mapping['clipIdToTimelineItemId'].pop(change['id'], None)
        result.removed += 1
        storage.save_mapping(root, project_id, episode_id, mapping)

    storage.save_mapping(root, project_id, episode_id, mapping)

    # Force the Edit page to actually display the timeline we just synced.
    # SetCurrentTimeline alone in _ensure_timeline updates project state but
    # Resolve 20's Edit page editor doesn't refresh until OpenPage is called.
    try:
        if hasattr(resolve, 'OpenPage'):
            resolve.OpenPage('edit')
        # Re-assert current timeline in case OpenPage reset it.
        project.SetCurrentTimeline(tl)
    except Exception:
        # Best-effort — if these calls fail the apply still succeeded; the
        # editor just won't auto-switch (user can double-click the timeline).
        pass

    return result
