"""File and frame navigation mixin for DICOMViewer.

Provides methods for moving between files in the selection list and
between frames within a multi-frame DICOM dataset.
"""


class NavigationMixin:
    """Mixin for file and frame navigation."""

    # ------------------------------------------------------------------
    # File navigation
    # ------------------------------------------------------------------

    def select_index(self, idx):
        """Select a file by index in ``filtered_files`` and load it.

        Args:
            idx (int): Zero-based index into ``filtered_files``. Ignored
                if out of range.
        """
        if idx < 0 or idx >= len(self.filtered_files):
            return
        self.current_index = idx
        path = self.filtered_files[idx]
        self._select_tree_item_by_path(path)

    def next_file(self):
        """Advance to the next file in ``filtered_files`` if possible."""
        if self.current_index < len(self.filtered_files) - 1:
            self.select_index(self.current_index + 1)

    def prev_file(self):
        """Go back to the previous file in ``filtered_files`` if possible."""
        if self.current_index > 0:
            self.select_index(self.current_index - 1)

    def _on_key_up(self, event):
        """Handle Up-arrow key: select the previous file.

        Args:
            event: Tkinter event object.
        """
        if self.current_index > 0:
            self.select_index(self.current_index - 1)

    def _on_key_down(self, event):
        """Handle Down-arrow key: select the next file.

        Args:
            event: Tkinter event object.
        """
        if self.current_index < len(self.filtered_files) - 1:
            self.select_index(self.current_index + 1)

    # ------------------------------------------------------------------
    # Frame navigation
    # ------------------------------------------------------------------

    def _update_frame_controls(self, current, total):
        """Synchronise frame navigation widgets to reflect the current frame.

        Updates the frame label, slider range/value and enable/disable
        state of the previous/next buttons.

        Args:
            current (int): One-based index of the currently selected frame.
            total (int): Total number of frames available.
        """
        current = int(current)
        total = int(total)
        self.frame_label.config(text=f"Frame {current}/{total}")

        if self.frame_slider is not None:
            self.frame_slider.configure(from_=1, to=max(1, total))
            cmd = self.frame_slider.cget("command")
            self.frame_slider.configure(command=None)
            try:
                self.frame_slider.set(current)
            finally:
                self.frame_slider.configure(command=cmd)

        if total <= 1:
            self.btn_frame_prev.configure(state="disabled")
            self.btn_frame_next.configure(state="disabled")
        else:
            self.btn_frame_prev.configure(
                state=("disabled" if current <= 1 else "normal")
            )
            self.btn_frame_next.configure(
                state=("disabled" if current >= total else "normal")
            )

    def next_frame(self):
        """Advance to the next frame in the current DICOM dataset."""
        if not self.current_ds:
            return
        total = int(getattr(self.current_ds, "NumberOfFrames", 1))
        if total <= 1:
            return
        if self.current_frame_index < total - 1:
            self.current_frame_index += 1
        self._clear_roi()
        self._update_frame_controls(self.current_frame_index + 1, total)
        self._render_image()
        if self.header_link_to_frame.get() and self.header_scope_var.get().startswith(
            "Frame"
        ):
            self._rebuild_header_tree()

    def prev_frame(self):
        """Go back to the previous frame in the current DICOM dataset."""
        if not self.current_ds:
            return
        total = int(getattr(self.current_ds, "NumberOfFrames", 1))
        if total <= 1:
            return
        if self.current_frame_index > 0:
            self.current_frame_index -= 1
        self._clear_roi()
        self._update_frame_controls(self.current_frame_index + 1, total)
        self._render_image()
        if self.header_link_to_frame.get() and self.header_scope_var.get().startswith(
            "Frame"
        ):
            self._rebuild_header_tree()

    def _on_frame_slider_change(self, value):
        """Handle user interaction with the frame slider.

        Args:
            value (float | str): Slider value as provided by Tkinter.
        """
        if not self.current_ds:
            return
        total = int(getattr(self.current_ds, "NumberOfFrames", 1))
        if total <= 1:
            return

        try:
            frame_num = int(round(float(value)))
        except Exception:
            return

        frame_num = max(1, min(total, frame_num))
        new_index = frame_num - 1

        if new_index == self.current_frame_index:
            return

        self._clear_roi()
        self.current_frame_index = new_index
        self._update_frame_controls(self.current_frame_index + 1, total)
        self._render_image()

        if self.header_link_to_frame.get() and self.header_scope_var.get().startswith(
            "Frame"
        ):
            self._rebuild_header_tree()

    def _on_frame_slider_wheel(self, event):
        """Handle mouse-wheel events on the frame slider.

        Args:
            event: Tkinter mouse wheel event.
        """
        if not self.current_ds:
            return
        total = int(getattr(self.current_ds, "NumberOfFrames", 1))
        if total <= 1:
            return

        if hasattr(event, "delta") and event.delta != 0:
            delta = 1 if event.delta < 0 else -1
        elif hasattr(event, "num"):
            delta = 1 if event.num == 5 else -1 if event.num == 4 else 0
        else:
            delta = 0

        if delta == 0:
            return

        new_index = self.current_frame_index + delta
        if 0 <= new_index < total:
            self.current_frame_index = new_index
            self._clear_roi()
            self._update_frame_controls(self.current_frame_index + 1, total)
            self._render_image()
            if (
                self.header_link_to_frame.get()
                and self.header_scope_var.get().startswith("Frame")
            ):
                self._rebuild_header_tree()

    def _on_mouse_wheel(self, event):
        """Handle canvas mouse-wheel events for frame navigation.

        Args:
            event: Tkinter mouse wheel event.
        """
        self._interactive_resize = True
        self._update_canvas_image()
        self.after(100, self._finish_interactive_zoom)
        if not self.current_ds:
            return
        total = int(getattr(self.current_ds, "NumberOfFrames", 1))
        if total <= 1:
            return
        delta = 1 if event.delta < 0 else -1
        self.current_frame_index = (self.current_frame_index + delta) % total
        self._update_frame_controls(self.current_frame_index + 1, total)
        self._render_image()
