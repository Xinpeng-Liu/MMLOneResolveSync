"""Minimal fake of DaVinci Resolve's scripting API. Just enough for unit tests.

Mirrors method names + return shapes documented in the Resolve scripting README
shipped with Resolve (`Documentation/Developer/Scripting/README.txt`). Where
there was ambiguity we pick the call signatures actually used by the production
code in `mml_sync/resolve_apply.py`, so a mismatch surfaces here instead of in
prod.

  Resolve.GetProjectManager() -> ProjectManager
  ProjectManager.GetCurrentProject() -> Project
  Project.GetMediaPool() -> MediaPool
  MediaPool.ImportMedia([path]) -> [MediaPoolItem]
  MediaPool.CreateEmptyTimeline(name) -> Timeline
  MediaPool.AppendToTimeline([clipInfo]) -> [TimelineItem]
"""
from typing import Any, Dict, List, Optional


class FakeMediaPoolItem:
    def __init__(self, path: str, item_id: str):
        self._path = path
        self._unique_id = item_id

    def GetUniqueId(self) -> str:
        return self._unique_id

    def GetClipProperty(self, name: str) -> str:
        if name == 'File Path':
            return self._path
        return ''


class FakeTimelineItem:
    def __init__(self, item_id: str, media: FakeMediaPoolItem,
                 source_start: int, source_end: int, record_start: int):
        self._unique_id = item_id
        self._media = media
        self._source_start = source_start
        self._source_end = source_end
        # `record_start` is the timeline frame the clip is placed at. This is
        # what `GetStart()` returns on real Resolve TimelineItems.
        self._record_start = record_start
        self._color: Optional[str] = None
        self._markers: List[Dict[str, Any]] = []

    def GetUniqueId(self) -> str:
        return self._unique_id

    def SetClipColor(self, name: str) -> bool:
        self._color = name
        return True

    def AddMarker(self, frame: int, color: str, name: str, note: str,
                  duration: int, custom_data: str = '') -> bool:
        self._markers.append({
            'frame': frame, 'color': color, 'name': name, 'note': note,
            'duration': duration, 'customData': custom_data,
        })
        return True

    def GetStart(self) -> int:
        return self._record_start

    def GetMediaPoolItem(self) -> FakeMediaPoolItem:
        return self._media


class FakeTimeline:
    def __init__(self, name: str, fps: float):
        self._name = name
        self._fps = fps
        self._track_items: Dict[str, List[FakeTimelineItem]] = {'video': [], 'audio': []}
        self._next_id = 0
        # Real Resolve timelines start at 01:00:00:00 (3600 seconds * fps).
        # Mirror that so tests catch off-by-startFrame mistakes.
        self._start_frame = int(round(fps * 3600))

    def GetName(self) -> str:
        return self._name

    def GetStartFrame(self) -> int:
        return self._start_frame

    def GetSetting(self, key: str) -> str:
        if key == 'timelineFrameRate':
            return str(self._fps)
        return ''

    def GetItemListInTrack(self, kind: str, index: int) -> List[FakeTimelineItem]:
        # Real Resolve indexes tracks 1..N; we route index>1 to a "{kind}-{index}" bucket.
        key = kind if index == 1 else f'{kind}-{index}'
        return list(self._track_items.get(key, []))

    def GetTrackCount(self, kind: str) -> int:
        n = 0
        for key in self._track_items:
            if key == kind:
                n = max(n, 1)
            elif key.startswith(f'{kind}-'):
                try:
                    n = max(n, int(key.split('-', 1)[1]))
                except ValueError:
                    pass
        return n

    def DeleteClips(self, items: List[FakeTimelineItem]) -> bool:
        targets = {it._unique_id for it in items}
        for key, lst in list(self._track_items.items()):
            self._track_items[key] = [it for it in lst if it._unique_id not in targets]
        return True

    def AddTrack(self, kind: str, _sub: Optional[str] = None) -> bool:
        # Real Resolve adds a new empty track at the next index. Mirror that
        # by inserting an empty bucket so subsequent GetTrackCount/AppendToTimeline
        # see the new track.
        existing = self.GetTrackCount(kind)
        new_index = existing + 1
        key = kind if new_index == 1 else f'{kind}-{new_index}'
        self._track_items.setdefault(key, [])
        return True


class FakeMediaPool:
    """Mirrors the production code path:
    - `ImportMedia([path])` → list of MediaPoolItem
    - `CreateEmptyTimeline(name)` → Timeline (real Resolve method on MediaPool)
    - `AppendToTimeline([{mediaPoolItem, startFrame, endFrame, recordFrame, trackIndex, mediaType}])`
    """

    def __init__(self):
        self._next_media = 0
        self._project: Optional['FakeProject'] = None  # set by FakeProject.__init__

    def ImportMedia(self, paths: List[str]) -> List[FakeMediaPoolItem]:
        out: List[FakeMediaPoolItem] = []
        for p in paths:
            self._next_media += 1
            out.append(FakeMediaPoolItem(p, f'mp_{self._next_media}'))
        return out

    def CreateEmptyTimeline(self, name: str) -> FakeTimeline:
        if self._project is None:
            raise RuntimeError('FakeMediaPool not bound to a project')
        t = FakeTimeline(name, self._project._fps)
        self._project._timelines.append(t)
        self._project._current = t
        return t

    def AppendToTimeline(self, clip_infos: List[Dict[str, Any]]) -> List[FakeTimelineItem]:
        if self._project is None or self._project._current is None:
            return []
        tl = self._project._current
        out: List[FakeTimelineItem] = []
        for info in clip_infos:
            track_index = int(info.get('trackIndex', 1))
            media_type = int(info.get('mediaType', 1))  # 1=video, 2=audio
            kind = 'video' if media_type == 1 else 'audio'
            key = kind if track_index == 1 else f'{kind}-{track_index}'
            tl._next_id += 1
            item = FakeTimelineItem(
                f'ti_{tl._next_id}',
                info['mediaPoolItem'],
                int(info.get('startFrame', 0)),
                int(info.get('endFrame', 0)),
                int(info.get('recordFrame', 0)),
            )
            tl._track_items.setdefault(key, []).append(item)
            out.append(item)
        return out


class FakeProject:
    def __init__(self, name: str = 'Untitled', fps: float = 30.0):
        self._name = name
        # Real Resolve does not let scripts look up timelines by name. The
        # in-order list mirrors `GetTimelineCount` / `GetTimelineByIndex`.
        self._timelines: List[FakeTimeline] = []
        self._current: Optional[FakeTimeline] = None
        self._media_pool = FakeMediaPool()
        self._media_pool._project = self
        self._fps = fps
        self._settings: Dict[str, str] = {'timelineFrameRate': str(fps)}

    def GetName(self) -> str:
        return self._name

    def GetMediaPool(self) -> FakeMediaPool:
        return self._media_pool

    def GetCurrentTimeline(self) -> Optional[FakeTimeline]:
        return self._current

    def SetCurrentTimeline(self, t: FakeTimeline) -> bool:
        self._current = t
        return True

    def GetTimelineCount(self) -> int:
        return len(self._timelines)

    def GetTimelineByIndex(self, index: int) -> Optional[FakeTimeline]:
        if index < 1 or index > len(self._timelines):
            return None
        return self._timelines[index - 1]

    def GetSetting(self, key: str) -> str:
        return self._settings.get(key, '')

    def SetSetting(self, key: str, value: str) -> bool:
        self._settings[key] = value
        if key == 'timelineFrameRate':
            self._fps = float(value)
            for t in self._timelines:
                t._fps = self._fps
        return True


class FakeProjectManager:
    def __init__(self, project: FakeProject):
        self._project = project

    def GetCurrentProject(self) -> FakeProject:
        return self._project


class FakeResolve:
    def __init__(self, project: FakeProject):
        self._pm = FakeProjectManager(project)

    def GetProjectManager(self) -> FakeProjectManager:
        return self._pm
