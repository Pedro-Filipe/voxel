"""Canvas overlay mixin for DICOMViewer.

Draws the crosshair/pixel-readout overlay, diffusion information overlay,
and basic DICOM metadata overlay on the image canvas.  Also handles
mouse-move and mouse-leave events for the crosshair.
"""

from pydicom.tag import Tag

from .utils_dicom import safe_str


class OverlayMixin:
    """Mixin providing all canvas overlay drawing and mouse-tracking logic."""

    # ------------------------------------------------------------------
    # Crosshair + pixel readout
    # ------------------------------------------------------------------

    def _draw_overlay(self):
        """Draw the crosshair and pixel-readout overlay on the canvas.

        Deletes any existing ``"overlay"`` canvas items, then — if the
        crosshair is enabled and the mouse is inside the image — draws
        horizontal and vertical crosshair lines plus a pixel-value label.
        """
        self.canvas.delete("overlay")

        if not self.show_crosshair.get() or self.current_image_pil is None:
            return
        if self._mouse_x_canvas is None or self._mouse_y_canvas is None:
            return

        ij = self._canvas_to_image_coords(self._mouse_x_canvas, self._mouse_y_canvas)
        bbox = self._get_image_bbox_on_canvas()
        if ij is None or bbox is None:
            self._update_cursor_label(None, None, None)
            return
        i, j = ij
        x0, y0, x1, y1, z = bbox

        cxp = x0 + (i + 0.5) * z
        cyp = y0 + (j + 0.5) * z

        color = "#ffff66"
        self.canvas.create_line(x0, cyp, x1, cyp, fill=color, width=1, tags="overlay")
        self.canvas.create_line(cxp, y0, cxp, y1, fill=color, width=1, tags="overlay")

        s = max(2, min(6, int(z / 2)))
        self.canvas.create_line(cxp - s, cyp, cxp + s, cyp, fill=color, tags="overlay")
        self.canvas.create_line(cxp, cyp - s, cxp, cyp + s, fill=color, tags="overlay")

        vals = self._get_pixel_values(i, j)
        text = vals["label"]
        tx = min(max(cxp + 10, 5), self.canvas.winfo_width() - 5)
        ty = min(max(cyp + 10, 5), self.canvas.winfo_height() - 5)
        txt_id = self.canvas.create_text(
            tx,
            ty,
            text=text,
            fill="white",
            anchor="nw",
            font=("TkDefaultFont", 8),
            tags="overlay",
        )
        try:
            xA, yA, xB, yB = self.canvas.bbox(txt_id)
            pad = 2
            rect_id = self.canvas.create_rectangle(
                xA - pad,
                yA - pad,
                xB + pad,
                yB + pad,
                fill="#000000",
                outline="",
                tags=("overlay",),
            )
            self.canvas.tag_lower(rect_id, txt_id)
        except Exception:
            pass

        self._update_cursor_label(i, j, vals)

    def _get_pixel_values(self, i, j):
        """Return pixel value information for image position *(i, j)*.

        Args:
            i (int): Column (x) index in image space.
            j (int): Row (y) index in image space.

        Returns:
            dict: Keys ``"SV"`` (stored value), optionally ``"DV"``
            (display value), ``"MV"`` (modality value), and ``"label"``
            (formatted string for overlay).
        """
        result = {"SV": "-", "label": f"x {i} y {j} | SV=-"}

        try:
            dv = self.current_image_pil.getpixel((i, j))
            result["DV"] = f"RGB{dv}" if isinstance(dv, tuple) else int(dv)
        except Exception:
            pass

        if not self._frame_is_color and self._frame_raw is not None:
            try:
                result["SV"] = int(self._frame_raw[j, i])
            except Exception:
                pass
            if self._frame_modality is not None:
                try:
                    mv = float(self._frame_modality[j, i])
                    modality = getattr(self.current_ds, "Modality", "").upper()
                    result["MV"] = (
                        f"{int(round(mv))} HU" if modality == "CT" else f"{mv:.2f}"
                    )
                except Exception:
                    pass
        elif self._frame_is_color and self._frame_raw is not None:
            try:
                rgb = tuple(int(x) for x in self._frame_raw[j, i])
                result["SV"] = f"RGB{rgb}"
                result["MV"] = "-"
            except Exception:
                pass

        result["label"] = f"x {i} y {j} | SV={result['SV']}"
        return result

    def _update_cursor_label(self, i, j, vals):
        """Update the toolbar cursor label.

        Args:
            i (int | None): Column index, or ``None`` when outside image.
            j (int | None): Row index, or ``None`` when outside image.
            vals (dict | None): Pixel value dict from ``_get_pixel_values``.
        """
        if i is None or j is None or vals is None:
            self.lbl_cursor.config(text="Cursor: x=- y=- | SV=-")
        else:
            self.lbl_cursor.config(text=f"Cursor: x {i} y {j} | SV={vals['SV']}")

    def _on_mouse_move(self, event):
        """Throttled callback for canvas mouse movement (≤50 FPS).

        Updates the crosshair and all overlays.

        Args:
            event: Tkinter motion event.
        """
        if self.current_image_pil is None:
            return
        now = self.winfo_toplevel().tk.call("clock", "milliseconds")
        try:
            now = int(now)
        except Exception:
            now = 0

        if now - self._last_mouse_redraw_ms < 20:
            return
        self._last_mouse_redraw_ms = now

        self._mouse_x_canvas = event.x
        self._mouse_y_canvas = event.y
        self._draw_overlay()
        self._redraw_roi_overlay()
        self._draw_diffusion_overlay()
        self._draw_basic_metadata_overlay()

    def _on_mouse_leave(self, event):
        """Clear crosshair and cursor readout when the mouse leaves the canvas.

        Args:
            event: Tkinter leave event.
        """
        self._mouse_x_canvas = None
        self._mouse_y_canvas = None
        self.canvas.delete("overlay")
        self._update_cursor_label(None, None, None)

    # ------------------------------------------------------------------
    # Diffusion overlay
    # ------------------------------------------------------------------

    def _get_diffusion_info_for_current_frame(self):
        """Extract diffusion metadata for the current frame.

        Searches per-frame functional groups, then shared functional
        groups, then the root dataset (for older vendor headers).

        Returns:
            dict[str, str] | None: ``{"b": ..., "dir": ..., "grad": ...}``
            or ``None`` if no diffusion info is found.
        """
        ds = self.current_ds
        if ds is None:
            return None

        # Per-frame first
        per_frame = getattr(ds, "PerFrameFunctionalGroupsSequence", None)
        mr = None
        if per_frame is not None and len(per_frame) > self.current_frame_index:
            pf_item = per_frame[self.current_frame_index]
            mr_seq = getattr(pf_item, "MRDiffusionSequence", None)
            if mr_seq and len(mr_seq) > 0:
                mr = mr_seq[0]

        # Shared fallback
        if mr is None:
            shared = getattr(ds, "SharedFunctionalGroupsSequence", None)
            if shared:
                for item in shared:
                    mr_seq = getattr(item, "MRDiffusionSequence", None)
                    if mr_seq and len(mr_seq) > 0:
                        mr = mr_seq[0]
                        break

        # Root dataset fallback (old Siemens headers)
        if mr is None:
            mr = ds

        def get_attr(d, name, tag_hex=None):
            val = getattr(d, name, None)
            if val is not None:
                return val
            if tag_hex is not None:
                try:
                    elem = d.get(Tag(tag_hex), None)
                    if elem is not None:
                        return elem.value
                except Exception:
                    pass
            return None

        bval = get_attr(mr, "DiffusionBValue", 0x00189087)
        if bval is None:
            bval = get_attr(mr, "[B_value]", 0x0019100C)

        dirality = get_attr(mr, "DiffusionDirectionality", 0x00189075)

        grad_vec = None
        grad_seq = get_attr(mr, "DiffusionGradientDirectionSequence", 0x00189089)
        if grad_seq is not None and isinstance(grad_seq, (list, tuple)):
            try:
                item = grad_seq[0]
                vec = get_attr(item, "DiffusionGradientOrientation", 0x00189090)
                if vec is None:
                    floats = []
                    for e in item:
                        try:
                            v = e.value
                            if isinstance(v, (float, int)):
                                floats.append(float(v))
                            elif isinstance(v, (list, tuple)) and all(
                                isinstance(x, (float, int)) for x in v
                            ):
                                floats.extend([float(x) for x in v])
                        except Exception:
                            pass
                    if len(floats) >= 3:
                        vec = floats[:3]
                if vec is not None:
                    grad_vec = tuple(
                        float(x)
                        for x in (vec if isinstance(vec, (list, tuple)) else [vec])
                    )
                    if len(grad_vec) == 1:
                        grad_vec = None
                    elif len(grad_vec) > 3:
                        grad_vec = grad_vec[:3]
            except Exception:
                pass
        else:
            vec = get_attr(mr, "DiffusionGradientOrientation", 0x00189090)
            if vec is not None:
                try:
                    grad_vec = tuple(
                        float(x)
                        for x in (vec if isinstance(vec, (list, tuple)) else [vec])
                    )
                    if len(grad_vec) > 3:
                        grad_vec = grad_vec[:3]
                except Exception:
                    pass

        b_str = (
            f"{bval:.1f}"
            if isinstance(bval, (int, float))
            else (str(bval) if bval is not None else "n/a")
        )
        d_str = str(dirality) if dirality is not None else "n/a"
        g_str = (
            f"[{grad_vec[0]:.3f}, {grad_vec[1]:.3f}, {grad_vec[2]:.3f}]"
            if grad_vec is not None and len(grad_vec) == 3
            else "n/a"
        )

        return {"b": b_str, "dir": d_str, "grad": g_str}

    def _draw_diffusion_overlay(self):
        """Draw diffusion b-value overlay in the bottom-left of the image."""
        if self.current_image_pil is None:
            return
        info = self._get_diffusion_info_for_current_frame()
        if info is None:
            return

        bbox = self._get_image_bbox_on_canvas()
        if bbox is None:
            return
        x0, y0, x1, y1, z = bbox

        pad = 8
        tx = max(5, int(x0) + pad)
        ty = int(y1) - pad

        text = f"Diffusion: b={info['b']}"
        txt_id = self.canvas.create_text(
            tx,
            ty,
            text=text,
            fill="#00e0ff",
            anchor="sw",
            font=("TkDefaultFont", 8, "bold"),
            tags="overlay",
        )
        try:
            xA, yA, xB, yB = self.canvas.bbox(txt_id)
            bg_pad = 3
            rect_id = self.canvas.create_rectangle(
                xA - bg_pad,
                yA - bg_pad,
                xB + bg_pad,
                yB + bg_pad,
                fill="#00222a",
                outline="#004455",
                width=1,
                tags="overlay",
            )
            self.canvas.tag_lower(rect_id, txt_id)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Basic metadata overlay
    # ------------------------------------------------------------------

    def _get_basic_metadata_lines(self):
        """Return a list of metadata strings for the top-right overlay.

        Returns:
            list[str]: Lines to display (empty list if no dataset loaded).
        """
        ds = self.current_ds
        if ds is None:
            return []
        total_frames = int(getattr(ds, "NumberOfFrames", 1))
        return [
            f"Image type: {safe_str(getattr(ds, 'ImageType', '')) or 'N/A'}",
            f"Study Date: {safe_str(getattr(ds, 'StudyDate', '')) or 'N/A'}",
            f"Series: {safe_str(getattr(ds, 'SeriesDescription', '')) or 'N/A'}",
            f"Series No: {safe_str(getattr(ds, 'SeriesNumber', '')) or 'N/A'}",
            f"Instance: {safe_str(getattr(ds, 'InstanceNumber', '')) or 'N/A'}",
            f"matrix: {safe_str(getattr(ds, 'Rows', '')) or 'N/A'} x {safe_str(getattr(ds, 'Columns', '')) or 'N/A'}",
            f"Frame: {self.current_frame_index + 1}/{total_frames}",
        ]

    def _draw_basic_metadata_overlay(self):
        """Draw basic DICOM metadata at the top-right of the image, stacked vertically."""
        if self.current_image_pil is None:
            return
        bbox = self._get_image_bbox_on_canvas()
        if bbox is None:
            return
        x0, y0, x1, y1, z = bbox

        lines = self._get_basic_metadata_lines()
        if not lines:
            return

        pad = 8
        line_gap = 2
        xR = int(x1) - pad
        y = max(5, int(y0) + pad)

        for line in lines:
            txt_id = self.canvas.create_text(
                xR,
                y,
                text=line,
                fill="#00e0ff",
                anchor="ne",
                font=("TkDefaultFont", 8, "bold"),
                tags="overlay",
            )
            try:
                xA, yA, xB, yB = self.canvas.bbox(txt_id)
                bg_pad = 3
                rect_id = self.canvas.create_rectangle(
                    xA - bg_pad,
                    yA - bg_pad,
                    xB + bg_pad,
                    yB + bg_pad,
                    fill="#000000",
                    outline="#303030",
                    width=1,
                    tags="overlay",
                )
                self.canvas.tag_lower(rect_id, txt_id)
                y = yB + line_gap
            except Exception:
                y += 16 + line_gap
