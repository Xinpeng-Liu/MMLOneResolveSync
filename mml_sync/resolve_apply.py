"""Translates a SyncDiff into Resolve API calls. Fakeable via duck typing.

`resolve` is anything that implements `GetProjectManager()` returning an object
with `GetCurrentProject()`. The fake in tests is duck-compatible with the real
`DaVinciResolveScript` `Resolve` instance.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

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

# Field sets that can be applied to an existing TimelineItem in place. A
# delete+re-add for these would destroy the editor's grade/effects while the
# re-added clip STILL wouldn't carry them — AppendToTimeline's clipInfo has
# no transform or volume fields.
_IMAGE_TRANSFORM_FIELDS = {'x', 'y', 'scaleX', 'scaleY', 'opacity', 'rotation'}
_VOLUME_FIELDS = {'volume'}


def modification_needs_media(change: Dict[str, Any]) -> bool:
    """Whether a modified diff entry will download/re-apply media, or be
    handled in place on the existing TimelineItem. The prefetch step uses
    this to skip media the apply loop never reads — a volume-only change on a
    video clip must not pull the whole source file. Transform-only image
    changes CAN still fall back to a re-apply when the tracked item vanished;
    that rare path self-heals with an inline download."""
    changed = set(change.get('fieldChanges') or [])
    if not changed:
        return True
    category = change.get('category')
    if category == 'image' and changed <= _IMAGE_TRANSFORM_FIELDS:
        return False
    if category in ('video', 'audio') and changed == _VOLUME_FIELDS:
        return False
    return True

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


def _video_track_index_map(current_state: Dict[str, Any]) -> Dict[str, int]:
    """Map MML ONE video trackId -> Resolve video track index (1-based).

    MML ONE `VideoTrack.order` is 0 at the bottom (the 'main' track); Resolve's
    V1 is also the bottom track. Sorting by `order` ascending therefore maps the
    bottom MML ONE track -> V1, the next -> V2, and so on. Clips whose trackId
    isn't listed fall back to V1, mirroring buildSyncTimelineState which keeps
    unknown-but-visible clips on the main track.

    `videoTracks` is absent on snapshots produced before multi-track sync
    shipped — an empty map then leaves every clip on V1 (the prior behaviour).
    """
    tracks = current_state.get('videoTracks') or []
    ordered = sorted(tracks, key=lambda t: t.get('order', 0))
    return {t['id']: i + 1 for i, t in enumerate(ordered) if t.get('id')}


def _ext_for(clip: Dict[str, Any]) -> str:
    name = (clip.get('mediaName') or '').lower()
    if '.' in name:
        return name.rsplit('.', 1)[-1]
    return _EXT_BY_MIME.get(clip.get('mediaMimeType') or '', 'bin')


def _ensure_timeline(project, timeline_name: str, min_video_tracks: int = 2,
                     expected_fps: Optional[float] = None):
    """Look up a timeline by name (Resolve has no GetTimelineByName, so we walk
    the index). Create it via MediaPool.CreateEmptyTimeline if absent.

    We also pre-allocate enough video tracks: one per MML ONE video track plus
    one on top for image overlays. Resolve's AppendToTimeline does NOT
    auto-create missing tracks, so without this any clip on a higher track
    would fail to place.

    A fresh timeline inherits the project frame rate. When `expected_fps`
    disagrees with it we try to flip the new timeline to a per-timeline custom
    frame rate — only possible while the timeline is still empty, and only on
    Resolve builds that expose timeline settings, hence best-effort. The
    caller re-reads the effective rate and decides whether to abort.
    """
    count = int(project.GetTimelineCount() or 0)
    for i in range(1, count + 1):
        tl = project.GetTimelineByIndex(i)
        if tl is not None and tl.GetName() == timeline_name:
            project.SetCurrentTimeline(tl)
            _ensure_minimum_tracks(tl, min_video_tracks)
            return tl
    tl = project.GetMediaPool().CreateEmptyTimeline(timeline_name)
    if tl is None:
        raise TimelineNotFound(f'Resolve refused to create timeline {timeline_name!r}')
    if expected_fps is not None:
        try:
            project_fps = float(project.GetSetting('timelineFrameRate') or expected_fps)
        except (TypeError, ValueError):
            project_fps = expected_fps
        if abs(project_fps - expected_fps) > 0.01:
            _try_set_timeline_fps(tl, expected_fps)
    project.SetCurrentTimeline(tl)
    _ensure_minimum_tracks(tl, min_video_tracks)
    return tl


def _try_set_timeline_fps(tl, fps: float) -> bool:
    """Flip a (new, empty) timeline to a custom frame rate. Resolve requires
    useCustomSettings before timelineFrameRate takes; both return False on
    builds/timelines that don't allow it, and older builds lack SetSetting
    entirely."""
    if not hasattr(tl, 'SetSetting'):
        return False
    fps_str = f'{fps:g}'
    try:
        if not tl.SetSetting('useCustomSettings', '1'):
            return False
        return bool(tl.SetSetting('timelineFrameRate', fps_str))
    except Exception:
        return False


def _ensure_minimum_tracks(tl, min_video_tracks: int = 2) -> None:
    """Make sure the timeline has at least `min_video_tracks` video tracks (one
    per MML ONE video track + one for image overlays) and A1 for audio. Resolve
    creates new timelines with V1 + A1 by default, so we add the rest."""
    try:
        video_count = int(tl.GetTrackCount('video') or 0)
    except (AttributeError, TypeError):
        video_count = 0
    while video_count < min_video_tracks:
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
    A/V clips.

    The source range is trimStart .. trimStart+duration regardless of clip
    speed: retime is not scriptable, so timeline LAYOUT correctness (the item
    occupies exactly `duration` on the timeline) is chosen over source-span
    correctness for speed≠1 clips — those get flagged via
    _flag_speed_mismatch for a manual retime."""
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
    """Scan every track on the timeline for a TimelineItem with the given id.
    GetItemListInTrack returns None (not []) on some Resolve builds/track
    indices, hence the `or []`."""
    for kind in ('video', 'audio'):
        try:
            count = int(tl.GetTrackCount(kind))
        except (AttributeError, TypeError):
            count = 16
        for idx in range(1, max(count, 1) + 1):
            for it in (tl.GetItemListInTrack(kind, idx) or []):
                if it.GetUniqueId() == unique_id:
                    return it
    return None


def _collect_timeline_item_ids(tl) -> set:
    """One pass over every video+audio track collecting each item's unique id.
    Membership checks against many tracked ids must use this instead of
    per-id _find_timeline_item — each of those is a full timeline walk of
    Resolve API round-trips."""
    ids: set = set()
    for kind in ('video', 'audio'):
        try:
            count = int(tl.GetTrackCount(kind))
        except (AttributeError, TypeError):
            count = 16
        for idx in range(1, max(count, 1) + 1):
            for it in (tl.GetItemListInTrack(kind, idx) or []):
                try:
                    ids.add(it.GetUniqueId())
                except (AttributeError, TypeError):
                    pass
    return ids


def _apply_transform(item, overlay: Dict[str, Any], canvas_w: float, canvas_h: float) -> bool:
    """Map an MML ONE overlay transform onto Resolve TimelineItem properties.

    Coordinate semantics mirror the FCPXML adapter
    (services/clip-composer/export/nle/adapters/fcpxml.ts, which targets
    "DaVinci Resolve 18+" and Resolve imports correctly):
      - MML x/y are the element CENTER as a canvas fraction, origin top-left,
        y growing DOWN (render-geometry.ts: centerX = x * canvasW,
        centerY = y * canvasH).
      - Resolve Pan/Tilt are pixel offsets from frame center with y growing
        UP (Inspector Position Y increases upward — the same convention the
        FCPXML `position` attribute encodes as py = (0.5 - y) * 100), hence
        Pan = (x - 0.5) * w and Tilt = (0.5 - y) * h.
      - scaleX/scaleY → ZoomX/ZoomY. For images MML scales relative to canvas
        width while Resolve zooms the fit-to-frame size; exact equivalence
        needs the media's natural size, which the sync state doesn't carry.
        The FCPXML adapter ships the same approximation.
      - rotation (degrees) → RotationAngle, passed through unchanged, again
        matching the FCPXML adapter.
      - opacity is 0..1 in MML (render-geometry multiplies alpha by it);
        Resolve Opacity is 0..100.
    Only fields present on the overlay are written. Returns True only when
    every attempted property was accepted.
    """
    if not hasattr(item, 'SetProperty'):
        return False
    props: Dict[str, float] = {}
    if overlay.get('x') is not None:
        props['Pan'] = (float(overlay['x']) - 0.5) * canvas_w
    if overlay.get('y') is not None:
        props['Tilt'] = (0.5 - float(overlay['y'])) * canvas_h
    if overlay.get('scaleX') is not None:
        props['ZoomX'] = float(overlay['scaleX'])
    if overlay.get('scaleY') is not None:
        props['ZoomY'] = float(overlay['scaleY'])
    if overlay.get('rotation') is not None:
        props['RotationAngle'] = float(overlay['rotation'])
    if overlay.get('opacity') is not None:
        props['Opacity'] = float(overlay['opacity']) * 100.0
    ok = True
    for key, value in props.items():
        try:
            if not item.SetProperty(key, value):
                ok = False
        except Exception:
            ok = False
    return ok


def _set_item_volume(item, volume) -> bool:
    """'Volume' as a scriptable TimelineItem property is not documented for
    every Resolve release — anything but an explicit truthy return counts as
    unsupported so the caller can warn instead of silently claiming success."""
    if volume is None or not hasattr(item, 'SetProperty'):
        return False
    try:
        return bool(item.SetProperty('Volume', float(volume)))
    except Exception:
        return False


def _flag_speed_mismatch(item, clip: Dict[str, Any], result: ApplyResult) -> None:
    """Retime cannot be set via the scripting API, so a speed≠1 clip lands at
    1x with correct timeline layout but the wrong playback rate. Mark it so
    the editor retimes by hand instead of discovering it in review."""
    try:
        speed = float(clip.get('speed', 1) or 1)
    except (TypeError, ValueError):
        return
    if abs(speed - 1.0) <= 1e-6:
        return
    try:
        item.AddMarker(
            frame=item.GetStart(), color='Blue',
            name='MML ONE: speed',
            note=f'Speed {speed:g}x in MML ONE — set retime manually in Resolve.',
            duration=1, custom_data='mml_one_speed',
        )
    except Exception:
        pass
    result.warnings.append(
        f'{clip.get("id", "clip")} plays at {speed:g}x in MML ONE — retime is not '
        'scriptable; set it manually in Resolve.'
    )


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

    # Land the imports in an 'MML ONE' bin so they don't scatter across the
    # editor's own media-pool organisation. Folder APIs vary across Resolve
    # builds, so every step is optional — any failure keeps the current folder.
    try:
        if hasattr(media_pool, 'GetRootFolder'):
            root_folder = media_pool.GetRootFolder()
            folder = None
            if root_folder is not None and hasattr(root_folder, 'GetSubFolderList'):
                for sub in (root_folder.GetSubFolderList() or []):
                    try:
                        if sub.GetName() == 'MML ONE':
                            folder = sub
                            break
                    except (AttributeError, TypeError):
                        pass
            if folder is None and root_folder is not None and hasattr(media_pool, 'AddSubFolder'):
                folder = media_pool.AddSubFolder(root_folder, 'MML ONE')
            if folder and hasattr(media_pool, 'SetCurrentFolder'):
                media_pool.SetCurrentFolder(folder)
    except Exception:
        pass

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

    if tl is None:
        missing_ids = set(tracked)
    else:
        # Single timeline walk instead of one _find_timeline_item scan per
        # tracked id — the latter is O(tracked × tracks × items) Resolve API
        # round-trips.
        present = _collect_timeline_item_ids(tl)
        missing_ids = {cid for cid, ti_id in tracked.items() if ti_id not in present}

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

    # MML ONE video tracks -> Resolve V1..Vn; image overlays sit one track
    # above all of them so they composite on top.
    track_index_map = _video_track_index_map(current_state)
    video_track_count = max(len(track_index_map), 1)
    image_track_index = video_track_count + 1

    tl = _ensure_timeline(project, timeline_name, min_video_tracks=image_track_index,
                          expected_fps=expected_fps)

    # The fps guard runs AFTER _ensure_timeline: a freshly created timeline
    # can be flipped to a per-timeline custom frame rate, which the project
    # setting alone can't express. Aborting here leaves at worst an empty
    # timeline behind — harmless, unlike clips placed at the wrong rate.
    effective_fps = project_fps
    try:
        raw = tl.GetSetting('timelineFrameRate') if hasattr(tl, 'GetSetting') else ''
        if raw:
            effective_fps = float(raw)
    except (AttributeError, TypeError, ValueError):
        pass
    if abs(effective_fps - expected_fps) > 0.01:
        raise FrameRateMismatch(
            f'Resolve timeline is {effective_fps:g} fps; MML ONE timeline is '
            f'{expected_fps:g} fps, and setting a custom timeline frame rate '
            'failed. Open or create a Resolve project whose frame rate matches '
            'the MML ONE timeline.'
        )
    media_pool = project.GetMediaPool()

    # Pan/Tilt are pixel offsets in timeline space; the transform math assumes
    # the Resolve timeline resolution equals the MML ONE canvas. A mismatch
    # shifts overlays proportionally — acceptable while both default 1920x1080.
    canvas_w = float(current_state.get('canvasWidth') or 1920)
    canvas_h = float(current_state.get('canvasHeight') or 1080)

    mapping = storage.load_mapping(root, project_id, episode_id)
    mapping['resolveProjectName'] = project.GetName()
    mapping['resolveTimelineName'] = timeline_name

    cache = config.media_cache_dir(root, project_id)
    cache.mkdir(parents=True, exist_ok=True)
    result = ApplyResult()

    # ---- added ----
    preexisting_ids: Optional[set] = None
    for change in diff['added']:
        category = change['category']
        clip = change['after']
        # Retry after a partial failure re-sends already-placed clips as
        # added (ui.py only advances the snapshot when skipped == 0). A clip
        # whose mapped TimelineItem is still on the timeline is done: no
        # download, no append, and NOT counted as skipped — an already-applied
        # item must not block the snapshot from advancing.
        mapped_ti = mapping['clipIdToTimelineItemId'].get(clip['id'])
        if mapped_ti:
            if preexisting_ids is None:
                preexisting_ids = _collect_timeline_item_ids(tl)
            if mapped_ti in preexisting_ids:
                continue
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
            # No mediaType → Resolve places video + audio together (Vn + A1),
            # matching the user's manual drag-from-pool expectation. Track index
            # comes from the clip's MML ONE trackId so multi-track timelines
            # don't collapse onto V1.
            item = _append_clip_for_clip(media_pool, tl, media=media, clip=clip,
                                         fps=expected_fps,
                                         track_index=track_index_map.get(clip.get('trackId'), 1))
        elif category == 'image':
            # Image overlays land one track above all video so they composite
            # on top. Transform is pushed onto the item afterwards via
            # SetProperty — AppendToTimeline's clipInfo has no transform fields.
            item = _append_overlay(media_pool, tl, media=media, overlay=clip,
                                   fps=expected_fps,
                                   track_index=image_track_index, media_type=_MEDIA_TYPE_VIDEO)
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
        if category == 'image':
            if not _apply_transform(item, clip, canvas_w, canvas_h):
                result.warnings.append(
                    f'Could not auto-apply transform for {clip["id"]} — adjust in Inspector'
                )
        elif category == 'video':
            _flag_speed_mismatch(item, clip, result)
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
        changed = set(change.get('fieldChanges') or [])

        # Transform-only image changes are applied to the existing item in
        # place. Either way the change counts as modified — skipped would
        # block the snapshot from advancing and re-present the same transform
        # on every sync. A vanished item falls through to the re-apply path.
        if category == 'image' and changed and changed <= _IMAGE_TRANSFORM_FIELDS:
            existing = _find_timeline_item(tl, ti_id) if ti_id else None
            if existing is not None:
                if _apply_transform(existing, clip, canvas_w, canvas_h):
                    existing.AddMarker(
                        frame=existing.GetStart(), color='Yellow',
                        name='MML ONE: transform updated',
                        note='fields: ' + ', '.join(sorted(changed)),
                        duration=1, custom_data='mml_one_transform',
                    )
                else:
                    result.warnings.append(
                        f'Transform for {change["id"]} changed in MML ONE but could not '
                        'be auto-applied — adjust in Inspector; clip and grade preserved'
                    )
                result.modified += 1
                continue

        # Volume-only changes never re-apply: AppendToTimeline cannot carry
        # volume either, so a delete+re-add would lose the editor's grade for
        # nothing. Consumed as modified even on failure, with a warning.
        if category in ('video', 'audio') and changed == _VOLUME_FIELDS:
            existing = _find_timeline_item(tl, ti_id) if ti_id else None
            if existing is not None and _set_item_volume(existing, clip.get('volume')):
                existing.AddMarker(
                    frame=existing.GetStart(), color='Yellow',
                    name='MML ONE: volume updated',
                    note=f'Volume {clip.get("volume")} in MML ONE.',
                    duration=1, custom_data='mml_one_volume',
                )
            else:
                result.warnings.append(
                    f'Volume for {change["id"]} changed in MML ONE — adjust manually; '
                    'clip preserved'
                )
            result.modified += 1
            continue

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
                                             fps=expected_fps,
                                             track_index=track_index_map.get(clip.get('trackId'), 1))
        elif category == 'image':
            new_item = _append_overlay(media_pool, tl, media=media, overlay=clip,
                                       fps=expected_fps,
                                       track_index=image_track_index, media_type=_MEDIA_TYPE_VIDEO)
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

        if category == 'image':
            # The re-add carries timing only; transform still has to be
            # pushed onto the fresh item.
            if not _apply_transform(new_item, clip, canvas_w, canvas_h):
                result.warnings.append(
                    f'Could not auto-apply transform for {change["id"]} — adjust in Inspector'
                )
        elif category == 'video':
            _flag_speed_mismatch(new_item, clip, result)

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
