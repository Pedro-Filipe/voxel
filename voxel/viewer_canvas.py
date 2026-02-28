"""Canvas rendering, zoom/pan and window-level mixin for DICOMViewer.

Handles image loading, display rendering, interactive zoom and pan, and
window/level adjustment via sliders and right-mouse dragging.
"""

import os

import numpy as np
from PIL import Image as PILImage, ImageTk

import pydicom
from pydicom.pixel_data_handlers.util import apply_modality_lut
from pydicom.multival import MultiValue
from tkinter import messagebox

from .utils_dicom import dicom_to_display_image


class CanvasMixin:
    """Mixin for image rendering, zoom/pan, and window-level controls."""

    # ------------------------------------------------------------------
    # DICOM file loading
    # ------------------------------------------------------------------

    def load_file(self, path):
        """Load a DICOM file, decode pixels, and display the image.

        Uses the metadata cache when available, reads the full dataset
        from disk otherwise. Resets ROI and viewing state, initialises
        default window/level, and renders the first frame.

        Args:
            path (str): Absolute path to the DICOM file to load.
        """
        ds = self.metadata_cache.get(path)
        try:
            if ds is None:
                ds = pydicom.dcmread(path, force=True)
                self.metadata_cache[path] = ds
            else:
                if "PixelData" not in ds:
                    ds = pydicom.dcmread(path, force=True)
                    self.metadata_cache[path] = ds
        except Exception as exc:
            messagebox.showerror(
                "Read Error", f"Failed to read DICOM file:\n{path}\n\n{exc}"
            )
            return

        self.current_ds = ds
        self.current_frame_index = 0

        # Cache pixel_array once per file (LRU)
        if path in self.pixel_cache:
            ds._cached_pixel_array = self.pixel_cache[path]
        else:
            try:
                arr = ds.pixel_array
                self.pixel_cache[path] = arr
                ds._cached_pixel_array = arr
            except Exception:
                ds._cached_pixel_array = None

        self._clear_roi()

        self._init_default_window_level(ds)
        self.window_center = self._default_window_center
        self.window_width = self._default_window_width
        self._sync_wl_controls()

        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0

        total_frames = int(getattr(ds, "NumberOfFrames", 1))
        self._update_frame_controls(1, total_frames)
        self.lbl_status.config(
            text=(
                f"[{self.current_index + 1}/{len(self.filtered_files)}] "
                f"{os.path.basename(path)} — "
                f"Patient: {getattr(ds, 'PatientName', 'N/A')} — "
                f"Study: {getattr(ds, 'StudyDescription', 'N/A')}"
            )
        )

        self._render_image()
        self._rebuild_header_tree()

    # ------------------------------------------------------------------
    # Window / level
    # ------------------------------------------------------------------

    def _sync_wl_controls(self):
        """Synchronise window/level sliders with the current WL values."""
        c = (
            self.window_center
            if self.window_center is not None
            else (self._default_window_center or 40.0)
        )
        w = (
            self.window_width
            if self.window_width is not None
            else (self._default_window_width or 400.0)
        )

        self.level_slider.configure(from_=c - 2000, to=c + 2000)
        self.window_slider.configure(from_=1, to=max(100, w * 4))
        self.level_slider.set(c)
        self.window_slider.set(w)

    def _init_default_window_level(self, ds):
        """Determine default window center/width for a dataset.

        Uses DICOM ``WindowCenter``/``WindowWidth`` when present; falls
        back to the 1st–99th percentile range of the pixel data.

        Args:
            ds (pydicom.Dataset): Dataset to derive defaults from.
        """
        wc = getattr(ds, "WindowCenter", None)
        ww = getattr(ds, "WindowWidth", None)

        if isinstance(wc, (MultiValue, list, tuple)):
            wc = wc[0]
        if isinstance(ww, (MultiValue, list, tuple)):
            ww = ww[0]

        if wc is not None and ww not in (None, 0):
            self._default_window_center = float(wc)
            self._default_window_width = float(ww)
            return

        try:
            arr = ds.pixel_array
            if arr.ndim >= 3 and getattr(ds, "NumberOfFrames", 1) > 1:
                arr = arr[0]
            arr = arr.astype(np.float32)
            try:
                arr = apply_modality_lut(arr, ds)
            except Exception:
                pass
            amin = float(np.percentile(arr, 1))
            amax = float(np.percentile(arr, 99))
            if amax <= amin:
                amax = amin + 1.0
            self._default_window_center = (amin + amax) / 2.0
            self._default_window_width = max(1.0, amax - amin)
        except Exception:
            self._default_window_center = 40.0
            self._default_window_width = 400.0

    def _on_window_change(self, value):
        try:
            self.window_width = max(1.0, float(value))
        except Exception:
            return
        self._render_image()

    def _on_level_change(self, value):
        try:
            self.window_center = float(value)
        except Exception:
            return
        self._render_image()

    def _on_reset_window_level(self):
        self.window_center = self._default_window_center
        self.window_width = self._default_window_width
        self._sync_wl_controls()
        self._render_image()

    def _on_wl_start(self, event):
        if self.current_ds is None:
            return
        self._wl_drag_start_x = event.x
        self._wl_drag_start_y = event.y
        self._wl_start_center = (
            self.window_center
            if self.window_center is not None
            else (self._default_window_center or 40.0)
        )
        self._wl_start_width = (
            self.window_width
            if self.window_width is not None
            else (self._default_window_width or 400.0)
        )
        if not self._wl_start_width or self._wl_start_width <= 0:
            self._wl_start_width = 400.0
        if self._wl_start_center is None:
            self._wl_start_center = 40.0

    def _on_wl_move(self, event):
        if self._wl_drag_start_x is None:
            return
        dx = event.x - self._wl_drag_start_x
        dy = event.y - self._wl_drag_start_y
        self.window_width = max(1.0, self._wl_start_width + dx * 2.0)
        self.window_center = self._wl_start_center - dy * 2.0
        self._sync_wl_controls()
        self._render_image()

    def _on_wl_end(self, event):
        self._wl_drag_start_x = None
        self._wl_drag_start_y = None
        self._wl_start_center = None
        self._wl_start_width = None

    # ------------------------------------------------------------------
    # Image rendering
    # ------------------------------------------------------------------

    def _render_image(self):
        """Render the current frame of the loaded DICOM dataset.

        Prepares per-frame cached arrays for pixel readout, then calls
        :func:`.utils_dicom.dicom_to_display_image` to obtain a
        display-ready PIL image and triggers a canvas redraw.
        """
        if not self.current_ds:
            return

        try:
            arr = getattr(self.current_ds, "_cached_pixel_array", None)
            if arr is None:
                arr = self.current_ds.pixel_array

            if arr.ndim >= 3 and getattr(self.current_ds, "NumberOfFrames", 1) > 1:
                frame = arr[self.current_frame_index]
            else:
                frame = arr

            self._frame_is_color = getattr(self.current_ds, "SamplesPerPixel", 1) == 3
            self._frame_raw = frame
            if self._frame_is_color:
                self._frame_modality = None
            else:
                try:
                    self._frame_modality = apply_modality_lut(frame, self.current_ds)
                except Exception:
                    self._frame_modality = None
        except Exception:
            self._frame_raw = None
            self._frame_modality = None
            self._frame_is_color = False

        try:
            pil_img = dicom_to_display_image(
                self.current_ds,
                frame_index=self.current_frame_index,
                window_center=self.window_center,
                window_width=self.window_width,
            )
        except Exception as exc:
            self.current_image_pil = None
            self.current_image_tk = None
            self.canvas.delete("all")
            w = max(100, self.canvas.winfo_width())
            h = max(100, self.canvas.winfo_height())
            self.canvas.create_text(
                w // 2,
                h // 2,
                text=f"Image render error:\n{exc}",
                fill="white",
            )
            return

        self.current_image_pil = pil_img
        self._update_canvas_image()

    def _update_canvas_image(self):
        """Redraw the current image and all overlays on the canvas.

        Resizes the PIL image according to the effective zoom, draws it
        on the canvas, and then draws all overlays on top.
        Uses bilinear resampling during interaction and Lanczos otherwise.
        """
        self.canvas.delete("all")
        if self.current_image_pil is None:
            return

        base_w, base_h = self.current_image_pil.size
        z = self._effective_zoom()

        zoomed_w = int(base_w * z)
        zoomed_h = int(base_h * z)
        if zoomed_w < 1 or zoomed_h < 1:
            return

        resample = PILImage.BILINEAR if self._interactive_resize else PILImage.LANCZOS
        zoomed_img = self.current_image_pil.resize((zoomed_w, zoomed_h), resample)
        self.current_image_tk = ImageTk.PhotoImage(zoomed_img)

        canvas_w = max(1, self.canvas.winfo_width())
        canvas_h = max(1, self.canvas.winfo_height())
        cx = canvas_w // 2
        cy = canvas_h // 2

        self.canvas.create_image(
            cx + self.pan_x,
            cy + self.pan_y,
            image=self.current_image_tk,
            anchor="center",
        )

        self._draw_overlay()
        self._redraw_roi_overlay()
        self._draw_diffusion_overlay()
        self._draw_basic_metadata_overlay()

    # ------------------------------------------------------------------
    # Canvas geometry helpers
    # ------------------------------------------------------------------

    def _effective_zoom(self):
        """Return effective zoom (canvas pixels per image pixel).

        Combines the fit-to-window scale with the user zoom factor.

        Returns:
            float: Effective zoom factor, clamped to a positive value.
        """
        if self.current_image_pil is None:
            return 1.0
        canvas_w = max(1, self.canvas.winfo_width())
        canvas_h = max(1, self.canvas.winfo_height())
        base_w, base_h = self.current_image_pil.size
        scale_to_fit = min(canvas_w / base_w, canvas_h / base_h)
        return max(0.0001, scale_to_fit * max(self.zoom, 0.1))

    def _get_image_bbox_on_canvas(self):
        """Return the image bounding box on the canvas and effective zoom.

        Returns:
            tuple[float, float, float, float, float] | None:
            ``(x0, y0, x1, y1, z)`` or ``None`` if no image is loaded.
        """
        if self.current_image_pil is None:
            return None

        base_w, base_h = self.current_image_pil.size
        z = self._effective_zoom()

        zoomed_w = int(base_w * z)
        zoomed_h = int(base_h * z)
        if zoomed_w < 1 or zoomed_h < 1:
            return None

        canvas_w = max(1, self.canvas.winfo_width())
        canvas_h = max(1, self.canvas.winfo_height())
        img_cx = canvas_w // 2 + self.pan_x
        img_cy = canvas_h // 2 + self.pan_y

        x0 = img_cx - zoomed_w / 2.0
        y0 = img_cy - zoomed_h / 2.0
        return (x0, y0, x0 + zoomed_w, y0 + zoomed_h, z)

    def _canvas_to_image_coords(self, x, y):
        """Convert canvas coordinates to image pixel indices.

        Args:
            x (float): X coordinate in canvas space.
            y (float): Y coordinate in canvas space.

        Returns:
            tuple[int, int] | None: ``(i, j)`` if inside image bounds,
            otherwise ``None``.
        """
        bbox = self._get_image_bbox_on_canvas()
        if bbox is None:
            return None
        x0, y0, x1, y1, z = bbox
        if z <= 0:
            return None

        i = int((x - x0) / z)
        j = int((y - y0) / z)

        if self.current_image_pil is None:
            return None
        W, H = self.current_image_pil.size
        if i < 0 or j < 0 or i >= W or j >= H:
            return None
        return (i, j)

    # ------------------------------------------------------------------
    # Zoom / pan
    # ------------------------------------------------------------------

    def _on_resize(self, event):
        if not self.current_ds:
            return
        self._update_canvas_image()

    def _on_mouse_wheel_zoom(self, event):
        """Handle mouse-wheel zoom centred on the mouse position.

        Args:
            event: Tkinter mouse wheel event.
        """
        self._interactive_resize = True
        self._update_canvas_image()
        self.after(100, self._finish_interactive_zoom)

        if self.current_image_pil is None:
            return

        if hasattr(event, "delta") and event.delta != 0:
            direction = 1 if event.delta > 0 else -1
        elif hasattr(event, "num"):
            direction = 1 if event.num == 4 else (-1 if event.num == 5 else 0)
        else:
            direction = 0

        if direction == 0:
            return

        old_zoom = self.zoom
        self.zoom *= 1.1 if direction > 0 else (1 / 1.1)
        self.zoom = max(0.1, min(self.zoom, 10.0))

        canvas_w = max(1, self.canvas.winfo_width())
        canvas_h = max(1, self.canvas.winfo_height())
        mx = event.x - canvas_w / 2.0
        my = event.y - canvas_h / 2.0

        if old_zoom != 0:
            scale = self.zoom / old_zoom
            self.pan_x = mx - (mx - self.pan_x) * scale
            self.pan_y = my - (my - self.pan_y) * scale

        self._update_canvas_image()

    def _finish_interactive_zoom(self):
        """Perform a final high-quality redraw after interactive zoom."""
        self._interactive_resize = False
        self._update_canvas_image()

    def _on_pan_start(self, event):
        """Record the start position for a pan gesture.

        Args:
            event: Tkinter mouse button press event.
        """
        self._interactive_resize = True
        if self.current_image_pil is None:
            return
        self._drag_start_x = event.x
        self._drag_start_y = event.y
        self._drag_start_pan_x = self.pan_x
        self._drag_start_pan_y = self.pan_y

    def _on_pan_move(self, event):
        """Update pan offsets while dragging.

        Args:
            event: Tkinter mouse motion event.
        """
        if self._drag_start_x is None or self.current_image_pil is None:
            return
        self.pan_x = self._drag_start_pan_x + (event.x - self._drag_start_x)
        self.pan_y = self._drag_start_pan_y + (event.y - self._drag_start_y)
        self._update_canvas_image()

    def _on_pan_end(self, event):
        """Finish a pan gesture with a final high-quality redraw.

        Args:
            event: Tkinter mouse button release event.
        """
        self._interactive_resize = False
        self._update_canvas_image()
        self._drag_start_x = None
        self._drag_start_y = None
        self._drag_start_pan_x = None
        self._drag_start_pan_y = None

    def _on_reset_zoom_pan(self, event):
        """Reset zoom and pan to defaults.

        Args:
            event: Tkinter mouse double-click event (unused).
        """
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self._update_canvas_image()
