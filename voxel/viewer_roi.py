"""ROI drawing and statistics mixin for DICOMViewer.

Handles the freehand ROI mode toggle, polygon drawing, mask generation,
statistical computation, and canvas overlay rendering for the ROI.
"""

import numpy as np
from PIL import Image as PILImage, ImageDraw


class ROIMixin:
    """Mixin for freehand ROI drawing, masking, and statistics."""

    # ------------------------------------------------------------------
    # ROI mode toggle
    # ------------------------------------------------------------------

    def _toggle_roi_button(self):
        """Toggle ROI drawing mode on or off."""
        self.roi_mode.set(not self.roi_mode.get())
        self._update_roi_button_appearance()
        self._on_toggle_roi_mode()

    def _update_roi_button_appearance(self):
        """Update the ROI toggle button label to reflect the current mode."""
        self.btn_roi_toggle.config(
            text="Exit ROI" if self.roi_mode.get() else "Draw ROI"
        )

    def _on_toggle_roi_mode(self):
        """Apply/remove ROI canvas bindings when the mode changes."""
        self._cancel_roi_draw()
        if self.roi_mode.get():
            self.canvas.unbind("<ButtonPress-1>")
            self.canvas.unbind("<B1-Motion>")
            self.canvas.unbind("<ButtonRelease-1>")
            self.canvas.bind("<ButtonPress-1>", self._on_roi_start)
            self.canvas.bind("<B1-Motion>", self._on_roi_draw)
            self.canvas.bind("<ButtonRelease-1>", self._on_roi_end)
            try:
                self.canvas.configure(cursor="pencil")
            except Exception:
                pass
        else:
            self.canvas.unbind("<ButtonPress-1>")
            self.canvas.unbind("<B1-Motion>")
            self.canvas.unbind("<ButtonRelease-1>")
            self.canvas.bind("<ButtonPress-1>", self._on_pan_start)
            self.canvas.bind("<B1-Motion>", self._on_pan_move)
            self.canvas.bind("<ButtonRelease-1>", self._on_pan_end)
            try:
                self.canvas.configure(cursor="")
            except Exception:
                pass

    # ------------------------------------------------------------------
    # ROI state management
    # ------------------------------------------------------------------

    def _cancel_roi_draw(self):
        """Cancel an in-progress ROI draw without committing it."""
        if self._roi_drawing:
            self._roi_drawing = False
            self.roi_points = []
            self._redraw_roi_overlay()

    def _clear_roi(self):
        """Clear the ROI completely, resetting all related state."""
        self._roi_drawing = False
        self.roi_points = []
        self.roi_mask = None
        self.roi_stats = None
        self._redraw_roi_overlay()
        self._update_roi_label()

    def _update_roi_label(self):
        """Update the toolbar ROI statistics label."""
        if not self.roi_stats:
            self.lbl_roi.config(text="ROI: N=0 μ=- σ=- med=- IQR=-")
            return
        s = self.roi_stats
        self.lbl_roi.config(
            text=(
                f"ROI: N={s['N']} μ={s['mean']:.2f} σ={s['std']:.2f} "
                f"med={s['median']:.2f} IQR={s['iqr']:.2f}"
            )
        )

    # ------------------------------------------------------------------
    # ROI drawing events
    # ------------------------------------------------------------------

    def _on_roi_start(self, event):
        """Start freehand ROI drawing on left-button press.

        Args:
            event: Tkinter mouse button press event.
        """
        if self.current_image_pil is None:
            return
        ij = self._canvas_to_image_coords(event.x, event.y)
        if ij is None:
            return
        self._roi_drawing = True
        self.roi_points = [ij]
        self.roi_mask = None
        self.roi_stats = None
        self._redraw_roi_overlay()

    def _on_roi_draw(self, event):
        """Append points to the ROI polyline while dragging.

        Args:
            event: Tkinter mouse motion event.
        """
        if not self._roi_drawing or self.current_image_pil is None:
            return
        ij = self._canvas_to_image_coords(event.x, event.y)
        if ij is None:
            return
        if not self.roi_points or (
            abs(ij[0] - self.roi_points[-1][0]) >= 1
            or abs(ij[1] - self.roi_points[-1][1]) >= 1
        ):
            self.roi_points.append(ij)
            self._redraw_roi_overlay()

    def _on_roi_end(self, event):
        """Finish ROI drawing on button release and finalise the polygon.

        Args:
            event: Tkinter mouse button release event.
        """
        if not self._roi_drawing:
            return
        self._roi_drawing = False
        if len(self.roi_points) >= 3:
            self._finalize_roi()
        else:
            self.roi_points = []
            self._redraw_roi_overlay()

    def _finalize_roi(self):
        """Build the binary ROI mask and compute statistics."""
        W, H = self.current_image_pil.size
        if len(self.roi_points) < 3:
            self.roi_mask = None
            self.roi_stats = None
            self._redraw_roi_overlay()
            self._update_roi_label()
            return

        try:
            mask_img = PILImage.new("L", (W, H), 0)
            draw = ImageDraw.Draw(mask_img)
            poly = [(int(x), int(y)) for (x, y) in self.roi_points]
            draw.polygon(poly, fill=1, outline=1)
            self.roi_mask = np.array(mask_img, dtype=bool)
        except Exception:
            self.roi_mask = None

        self.roi_stats = self._compute_roi_stats()
        self._redraw_roi_overlay()
        self._update_roi_label()

    # ------------------------------------------------------------------
    # ROI statistics
    # ------------------------------------------------------------------

    def _get_scalar_frame_for_stats(self):
        """Return a 2-D float array of scalar pixel values for statistics.

        For colour images returns the luminance channel.

        Returns:
            numpy.ndarray | None: 2-D float32 array or ``None``.
        """
        if self._frame_is_color and self._frame_raw is not None:
            arr = self._frame_raw.astype(np.float32)
            if arr.ndim == 3 and arr.shape[2] >= 3:
                return (
                    0.2126 * arr[..., 0] + 0.7152 * arr[..., 1] + 0.0722 * arr[..., 2]
                )
            return arr.mean(axis=-1) if arr.ndim == 3 else arr

        if self._frame_modality is not None:
            return self._frame_modality.astype(np.float32)
        if self._frame_raw is not None:
            return self._frame_raw.astype(np.float32)
        return None

    def _compute_roi_stats(self):
        """Compute mean, std, median and IQR for pixels inside the ROI.

        Returns:
            dict | None: Keys ``"N"``, ``"mean"``, ``"std"``,
            ``"median"``, ``"iqr"``, or ``None`` on failure.
        """
        if self.roi_mask is None:
            return None
        arr = self._get_scalar_frame_for_stats()
        if arr is None:
            return None

        try:
            roi_vals = arr[self.roi_mask]
            roi_vals = roi_vals[np.isfinite(roi_vals)]
            N = int(roi_vals.size)
            if N == 0:
                return None
            q1, med, q3 = np.percentile(roi_vals, [25, 50, 75])
            return {
                "N": N,
                "mean": float(np.mean(roi_vals)),
                "std": float(np.std(roi_vals)),
                "median": float(med),
                "iqr": float(q3 - q1),
            }
        except Exception:
            return None

    # ------------------------------------------------------------------
    # ROI canvas overlay
    # ------------------------------------------------------------------

    def _image_to_canvas_points(self, pts):
        """Convert image-space ``(i, j)`` points to canvas coordinates.

        Args:
            pts (list[tuple[int, int]]): Image-space points.

        Returns:
            list[tuple[float, float]]: Canvas-space points.
        """
        bbox = self._get_image_bbox_on_canvas()
        if bbox is None:
            return []
        x0, y0, x1, y1, z = bbox
        return [(x0 + (i + 0.5) * z, y0 + (j + 0.5) * z) for i, j in pts]

    def _redraw_roi_overlay(self):
        """Redraw the ROI polygon or polyline on the canvas."""
        for it in getattr(self, "_roi_items", []):
            try:
                self.canvas.delete(it)
            except Exception:
                pass
        self._roi_items = []

        if not self.roi_points:
            return

        pts_canvas = self._image_to_canvas_points(self.roi_points)
        if len(pts_canvas) < 2:
            return

        # In-progress: draw polyline
        if self._roi_drawing:
            for k in range(1, len(pts_canvas)):
                x0c, y0c = pts_canvas[k - 1]
                x1c, y1c = pts_canvas[k]
                self._roi_items.append(
                    self.canvas.create_line(
                        x0c, y0c, x1c, y1c, fill="#ff8800", width=2, tags=("roi",)
                    )
                )
            return

        # Finalised: filled polygon
        flat = [c for xy in pts_canvas for c in xy]
        try:
            poly_id = self.canvas.create_polygon(
                *flat,
                fill="#ffff00",
                outline="#ff8800",
                width=2,
                tags=("roi",),
            )
            try:
                self.canvas.itemconfigure(poly_id, stipple="gray25")
            except Exception:
                pass
            self._roi_items.append(poly_id)
        except Exception:
            for k in range(len(pts_canvas)):
                x0c, y0c = pts_canvas[k]
                x1c, y1c = pts_canvas[(k + 1) % len(pts_canvas)]
                self._roi_items.append(
                    self.canvas.create_line(
                        x0c, y0c, x1c, y1c, fill="#ff8800", width=2, tags=("roi",)
                    )
                )

        # Stats box at polygon centroid
        try:
            cx = sum(p[0] for p in pts_canvas) / len(pts_canvas)
            cy = sum(p[1] for p in pts_canvas) / len(pts_canvas)
            if self.roi_stats:
                s = self.roi_stats
                text = (
                    f"N={s['N']}  μ={s['mean']:.2f}  σ={s['std']:.2f}\n"
                    f"med={s['median']:.2f}  IQR={s['iqr']:.2f}"
                )
                tid = self.canvas.create_text(
                    cx,
                    cy,
                    text=text,
                    fill="black",
                    anchor="n",
                    font=("TkDefaultFont", 8),
                    tags=("roi",),
                )
                xA, yA, xB, yB = self.canvas.bbox(tid)
                pad = 3
                rid = self.canvas.create_rectangle(
                    xA - pad,
                    yA - pad,
                    xB + pad,
                    yB + pad,
                    fill="#ffffcc",
                    outline="#aa9955",
                    tags=("roi",),
                )
                self.canvas.tag_lower(rid, tid)
                self._roi_items.extend([tid, rid])
        except Exception:
            pass
