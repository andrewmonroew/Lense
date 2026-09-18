class UndoManager:
    """Snapshot-based undo/redo: before each undoable action, the whole project state
    is captured (via MainWindow._serialize_state(), the same dict .lense files are
    made of, just kept in memory) and pushed onto the undo stack. Undo pops it back
    and restores the scene from it; redo does the reverse.

    A snapshot per action rather than per-field reverse-commands is a deliberate
    trade-off: it can't be wrong in some subtle way a hand-written "undo this specific
    mutation" method could be, since it reuses the exact serialize/restore machinery
    that project save/load already depends on and that gets exercised constantly. The
    cost is granularity -- this only wraps discrete, deliberately-marked actions
    (placement, deletion, moves, rack mount/unmount, patch cable create/delete, field-
    cable routing), not continuous edits like typing in a label or dragging a slider."""

    MAX_DEPTH = 50

    def __init__(self, main_window):
        self.main_window = main_window
        self.undo_stack = []
        self.redo_stack = []
        self._suspended = False

    def snapshot(self):
        """Call BEFORE a mutating action to record the state to return to on undo --
        the simple/immediate case, for actions that are a single discrete method call
        (placement, deletion, mount/unmount, patch cable create/delete, ...). A no-op
        while a restore is already in progress, so undo/redo itself never pushes a
        spurious entry onto either stack."""
        if self._suspended:
            return
        self.commit_raw_snapshot(self.main_window._serialize_state())

    def commit_raw_snapshot(self, state):
        """Pushes an ALREADY-CAPTURED state (see CanvasView.capture_undo_state) onto
        the undo stack -- the two-step counterpart to snapshot(), needed for a drag:
        the pre-drag state has to be captured at mouse-press time, before anything
        moves, but whether it's worth an undo entry at all isn't known until release
        (did the item actually end up somewhere different?). Skipped entirely while a
        restore is in progress, same as snapshot()."""
        if state is None or self._suspended:
            return
        self.undo_stack.append(state)
        if len(self.undo_stack) > self.MAX_DEPTH:
            self.undo_stack.pop(0)
        self.redo_stack.clear()  # a fresh action invalidates whatever was available to redo

    def can_undo(self):
        return bool(self.undo_stack)

    def can_redo(self):
        return bool(self.redo_stack)

    def undo(self):
        if not self.undo_stack:
            return
        self._suspended = True
        try:
            current = self.main_window._serialize_state()
            previous = self.undo_stack.pop()
            self.redo_stack.append(current)
            self.main_window._replace_scene_state(previous)
        finally:
            self._suspended = False

    def redo(self):
        if not self.redo_stack:
            return
        self._suspended = True
        try:
            current = self.main_window._serialize_state()
            next_state = self.redo_stack.pop()
            self.undo_stack.append(current)
            self.main_window._replace_scene_state(next_state)
        finally:
            self._suspended = False
