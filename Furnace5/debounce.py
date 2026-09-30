"""
ClassDebouncer
--------------
YOLO detections can flicker frame to frame (a class appears for one frame,
disappears, reappears). Feeding that raw signal straight into the process
state machine would cause false stage changes.

This tracks, per class ID, how many *consecutive* processed frames it has
been seen in. A class only becomes "confirmed" once it has been seen for
`confirm_frames` frames in a row, and `update()` reports it back exactly
once at the moment it flips from unconfirmed -> confirmed (an "edge"),
plus once more when it flips back from confirmed -> unconfirmed ("lost").

This module is pure stdlib so it can be unit tested without OpenCV /
ultralytics installed.
"""

from collections import defaultdict


class ClassDebouncer:
    def __init__(self, confirm_frames=5, class_ids=None):
        """
        confirm_frames: number of consecutive frames required to confirm.
        class_ids: optional iterable restricting which class IDs are
                   tracked at all (saves memory / avoids noise from
                   classes we don't care about). None = track everything.
        """
        self.confirm_frames = max(1, int(confirm_frames))
        self.class_ids = set(class_ids) if class_ids is not None else None
        self._counts = defaultdict(int)
        self._confirmed = set()

    def update(self, detected_ids):
        """
        detected_ids: set/iterable of class IDs seen in the current frame.

        Returns (newly_confirmed, newly_lost) - both sets of class IDs.
        """
        detected_ids = set(detected_ids)
        if self.class_ids is not None:
            detected_ids = detected_ids & self.class_ids

        newly_confirmed = set()
        newly_lost = set()

        # Classes we need to check this round: anything detected now, plus
        # anything we were already tracking a streak for (so it can reset).
        candidates = detected_ids | set(self._counts.keys())

        for cid in candidates:
            if cid in detected_ids:
                self._counts[cid] += 1
                if self._counts[cid] >= self.confirm_frames and cid not in self._confirmed:
                    self._confirmed.add(cid)
                    newly_confirmed.add(cid)
            else:
                if self._counts.get(cid, 0) != 0:
                    self._counts[cid] = 0
                if cid in self._confirmed:
                    self._confirmed.discard(cid)
                    newly_lost.add(cid)

        return newly_confirmed, newly_lost

    def is_confirmed(self, class_id):
        return class_id in self._confirmed

    def confirmed_classes(self):
        return set(self._confirmed)
