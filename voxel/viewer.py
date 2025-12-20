import os
import threading
import sys

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from PIL import Image, ImageTk, Image as PILImage
import numpy as np

try:
    import pydicom
    from pydicom.pixel_data_handlers.util import (
        apply_voi_lut,
        apply_modality_lut,
        convert_color_space,
    )
    from pydicom.tag import Tag
except ImportError:
    raise SystemExit("Please install pydicom: pip install pydicom")

from .constants import APP_NAME, APP_VERSION, APP_COPYRIGHT
from .lru_cache import LRUCache
from .utils_dicom import (
    is_dicom_file,
    dicom_to_display_image,
    format_tag,
    safe_str,
)


# ----------------------------
# GUI Application
# ----------------------------
class DICOMViewer(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1200x800")
        self.minsize(900, 600)

        # Load icon from package / bundled app
        try:
            if getattr(sys, "frozen", False):
                base_dir = sys._MEIPASS  # type: ignore[attr-defined]
            else:
                base_dir = os.path.dirname(os.path.abspath(__file__))

            icon_path = os.path.join(base_dir, "assets", "icon.png")
            icon_img = PILImage.open(icon_path)
            self.iconphoto(False, ImageTk.PhotoImage(icon_img))
        except Exception as e:
            print(f"Icon load failed: {e}")

        # Cache minimal metadata for each file to avoid repeated dcmread
        # { path: dataset_without_pixels }
        self.metadata_cache = {}
        self.pixel_cache = LRUCache(max_items=8)

        self._last_mouse_redraw_ms = 0

        self._header_filter_after_id = None

        self.folder = None
        self.files = []  # full list (absolute paths)
        self.filtered_files = []  # filtered list (absolute paths)
        self.current_index = -1  # index in filtered_files
        self.current_ds = None

        self._interactive_resize = False

        # Header scope and expand toggle
        self.header_scope_var = tk.StringVar(value="Dataset")
        self.header_link_to_frame = tk.BooleanVar(value=True)
        self.header_expand_all = tk.BooleanVar(value=True)

        # NEW: File tree expand toggle (left panel)
        self.file_expand_all = tk.BooleanVar(value=False)  # default expanded

        # Hierarchical DICOM index: study -> series -> instances
        # self.series_hierarchy: {
        #   study_key: {
        #       "study_desc": str,
        #       "series": {
        #           series_key: {
        #               "series_desc": str,
        #               "series_number": any,
        #               "instances": [ { "path": str,
        #                                "instance_number": any,
        #                                "sop_instance_uid": str } ]
        #           }
        #       }
        #   }
        # }
        self.series_hierarchy = {}
        # Map Treeview item id -> file path for leaf nodes
        self.tree_item_to_path = {}

        # Image state
        self.current_image_pil = None  # full-resolution PIL image
        self.current_image_tk = None
        self.current_frame_index = 0

        # Zoom/pan (zoom is relative to fit-to-window scale)
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self._drag_start_x = None
        self._drag_start_y = None
        self._drag_start_pan_x = None
        self._drag_start_pan_y = None

        # Window/Level
        self.window_center = None  # None means "auto"
        self.window_width = None
        self._default_window_center = None
        self._default_window_width = None

        # WL drag
        self._wl_drag_start_x = None
        self._wl_drag_start_y = None
        self._wl_start_center = None
        self._wl_start_width = None

        # Crosshair / cursor
        self._mouse_x_canvas = None
        self._mouse_y_canvas = None
        self.show_crosshair = tk.BooleanVar(value=True)

        # Pixel readout caches
        self._frame_raw = None
        self._frame_modality = None
        self._frame_is_color = False

        # Filters
        self.file_filter_var = tk.StringVar()
        self.header_filter_var = tk.StringVar()

        # Header scope and expand toggle
        self.header_scope_var = tk.StringVar(value="Dataset")
        self.header_link_to_frame = tk.BooleanVar(value=True)
        self.header_expand_all = tk.BooleanVar(value=True)

        # Frame control widgets (buttons + label)
        self.btn_frame_prev = None
        self.btn_frame_next = None
        self.frame_label = None

        # ---- ROI state ----
        self.roi_mode = tk.BooleanVar(value=False)  # toggle for freehand ROI
        self.roi_points = []  # list of (i, j) in image coords
        self._roi_drawing = False
        self.roi_mask = None  # numpy bool mask (H, W)
        self.roi_stats = None  # dict with stats
        self._roi_items = []  # canvas item ids belonging to ROI overlay

        self._build_ui()
        self._bind_keys()

    def _build_ui(self):
        # Menu
        menubar = tk.Menu(self)
        filemenu = tk.Menu(menubar, tearoff=0)
        filemenu.add_command(label="Open Folder\tCtrl+O", command=self.open_folder)
        filemenu.add_separator()
        filemenu.add_command(label="Exit", command=self.quit)
        menubar.add_cascade(label="File", menu=filemenu)
        self.config(menu=menubar)

        # Help menu (new)
        helpmenu = tk.Menu(menubar, tearoff=0)
        helpmenu.add_command(label=f"About {APP_NAME}", command=self._show_about)
        menubar.add_cascade(label="Help", menu=helpmenu)

        self.config(menu=menubar)

        # Toolbar
        toolbar = ttk.Frame(self)
        toolbar.pack(side=tk.TOP, fill=tk.X, padx=5, pady=5)

        self.btn_open = ttk.Button(
            toolbar, text="Open Folder", command=self.open_folder
        )
        self.btn_prev = ttk.Button(toolbar, text="◀ Prev", command=self.prev_file)
        self.btn_next = ttk.Button(toolbar, text="Next ▶", command=self.next_file)
        self.chk_crosshair = ttk.Checkbutton(
            toolbar,
            text="Crosshair",
            variable=self.show_crosshair,
            command=self._update_canvas_image,
        )

        # ROI controls: Button toggles ROI mode (Enter/Exit)
        self.btn_roi_toggle = ttk.Button(
            toolbar, text="Draw ROI", command=self._toggle_roi_button
        )
        self.btn_roi_clear = ttk.Button(
            toolbar, text="Clear ROI", command=self._clear_roi
        )
        self.lbl_roi = ttk.Label(toolbar, text="ROI: N=0 μ=- σ=- med=- IQR=-")

        self.lbl_cursor = ttk.Label(toolbar, text="Cursor: x=- y=- | SV=-")
        self.lbl_status = ttk.Label(toolbar, text="No folder opened")

        # Left side controls
        self.btn_open.pack(side=tk.LEFT, padx=(0, 6))
        self.btn_prev.pack(side=tk.LEFT, padx=3)
        self.btn_next.pack(side=tk.LEFT, padx=3)
        self.chk_crosshair.pack(side=tk.LEFT, padx=(12, 6))
        self.btn_roi_toggle.pack(side=tk.LEFT, padx=(6, 3))
        self.btn_roi_clear.pack(side=tk.LEFT, padx=(3, 6))

        # Right side status/info
        self.lbl_roi.pack(side=tk.RIGHT, padx=(12, 0))
        self.lbl_cursor.pack(side=tk.RIGHT, padx=(12, 0))
        self.lbl_status.pack(side=tk.RIGHT)

        # Main Paned Window
        main_pane = ttk.Panedwindow(self, orient=tk.HORIZONTAL)
        main_pane.pack(fill=tk.BOTH, expand=True)

        # Left: file list + filter
        left_frame = ttk.Frame(main_pane)
        main_pane.add(left_frame, weight=1)

        ttk.Label(left_frame, text="DICOM Series / Instances").pack(
            anchor="w", padx=6, pady=(6, 0)
        )

        file_filter_frame = ttk.Frame(left_frame)
        file_filter_frame.pack(fill=tk.X, padx=6, pady=(4, 0))
        ttk.Label(file_filter_frame, text="Filter files:").pack(side=tk.LEFT)
        self.entry_file_filter = ttk.Entry(
            file_filter_frame, textvariable=self.file_filter_var
        )
        self.entry_file_filter.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 6))
        ttk.Button(
            file_filter_frame, text="Clear", command=self._clear_file_filter
        ).pack(side=tk.LEFT)
        self.entry_file_filter.bind("<KeyRelease>", self._on_file_filter_change)

        # After file_filter_frame in left_frame
        file_tree_ctrl_frame = ttk.Frame(left_frame)
        file_tree_ctrl_frame.pack(fill=tk.X, padx=6, pady=(4, 0))

        ttk.Checkbutton(
            file_tree_ctrl_frame,
            text="Expand all",
            variable=self.file_expand_all,
            command=self._on_file_expand_all_toggle,
        ).pack(side=tk.LEFT)

        # Hierarchical Treeview for Study → Series → Instance
        self.file_tree = ttk.Treeview(
            left_frame,
            columns=("Info",),
            show="tree headings",
            selectmode="browse",
        )
        self.file_tree.heading("#0", text="Study / Series / Instance")
        self.file_tree.column("#0", width=260, anchor="w")
        self.file_tree.heading("Info", text="Info")
        self.file_tree.column("Info", width=200, anchor="w")

        file_tree_vsb = ttk.Scrollbar(
            left_frame, orient="vertical", command=self.file_tree.yview
        )
        self.file_tree.configure(yscrollcommand=file_tree_vsb.set)

        self.file_tree.pack(
            side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(6, 0), pady=6
        )
        file_tree_vsb.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 6), pady=6)

        self.file_tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        # Right: image + header
        right_pane = ttk.Panedwindow(main_pane, orient=tk.VERTICAL)
        main_pane.add(right_pane, weight=3)

        # Image area
        img_frame = ttk.Frame(right_pane)
        right_pane.add(img_frame, weight=3)

        self.canvas = tk.Canvas(img_frame, bg="#222222")
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<Configure>", self._on_resize)

        # Initial default: pan on LMB (ROI toggle can rebind)
        self.canvas.bind("<ButtonPress-1>", self._on_pan_start)
        self.canvas.bind("<B1-Motion>", self._on_pan_move)
        self.canvas.bind("<ButtonRelease-1>", self._on_pan_end)
        self.canvas.bind("<MouseWheel>", self._on_mouse_wheel_zoom)
        self.canvas.bind("<Button-4>", self._on_mouse_wheel_zoom)
        self.canvas.bind("<Button-5>", self._on_mouse_wheel_zoom)
        self.canvas.bind("<Double-Button-1>", self._on_reset_zoom_pan)

        self.canvas.bind("<ButtonPress-3>", self._on_wl_start)
        self.canvas.bind("<B3-Motion>", self._on_wl_move)
        self.canvas.bind("<ButtonRelease-3>", self._on_wl_end)

        self.canvas.bind("<Motion>", self._on_mouse_move)
        self.canvas.bind("<Leave>", self._on_mouse_leave)

        # --- Frame controls + Window/Level (single horizontal row) ---
        frame_ctrl = ttk.Frame(img_frame)
        frame_ctrl.pack(fill=tk.X, padx=6, pady=(2, 6))

        # Frame controls
        self.btn_frame_prev = ttk.Button(
            frame_ctrl, text="Frame ◀ Prev", command=self.prev_frame
        )
        self.btn_frame_prev.pack(side=tk.LEFT)

        self.frame_label = ttk.Label(frame_ctrl, text="Frame 1/1")
        self.frame_label.pack(side=tk.LEFT, padx=8)

        # NEW: frame slider
        self.frame_slider = ttk.Scale(
            frame_ctrl,
            from_=1,
            to=1,
            orient=tk.HORIZONTAL,
            command=self._on_frame_slider_change,
        )
        self.frame_slider.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)

        self.frame_slider.bind("<MouseWheel>", self._on_frame_slider_wheel)
        self.frame_slider.bind("<Button-4>", self._on_frame_slider_wheel)  # Linux
        self.frame_slider.bind("<Button-5>", self._on_frame_slider_wheel)  # Linux

        self.btn_frame_next = ttk.Button(
            frame_ctrl, text="Next ▶ Frame", command=self.next_frame
        )
        self.btn_frame_next.pack(side=tk.LEFT)

        # A small vertical separator between frame controls and WL controls
        ttk.Separator(frame_ctrl, orient="vertical").pack(
            side=tk.LEFT, fill=tk.Y, padx=8, pady=2
        )

        # Inline WL controls container (expands to fill remaining space)
        wl_inline = ttk.Frame(frame_ctrl)
        wl_inline.pack(side=tk.LEFT, fill=tk.X, expand=True)

        ttk.Label(wl_inline, text="Window:").pack(side=tk.LEFT)
        self.window_slider = ttk.Scale(
            wl_inline,
            from_=1,
            to=4000,
            orient=tk.HORIZONTAL,
            command=self._on_window_change,
        )
        self.window_slider.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)

        ttk.Label(wl_inline, text="Level:").pack(side=tk.LEFT)
        self.level_slider = ttk.Scale(
            wl_inline,
            from_=-1000,
            to=3000,
            orient=tk.HORIZONTAL,
            command=self._on_level_change,
        )
        self.level_slider.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)

        self.btn_reset_wl = ttk.Button(
            wl_inline, text="Reset WL", command=self._on_reset_window_level
        )
        self.btn_reset_wl.pack(side=tk.LEFT, padx=(6, 0))

        # Header area
        hdr_frame = ttk.Frame(right_pane)
        right_pane.add(hdr_frame, weight=2)

        ttk.Label(hdr_frame, text="DICOM Header").pack(anchor="w", padx=6, pady=(6, 0))

        header_filter_frame = ttk.Frame(hdr_frame)
        header_filter_frame.pack(fill=tk.X, padx=6, pady=(4, 0))
        ttk.Label(header_filter_frame, text="Filter header:").pack(side=tk.LEFT)
        self.entry_header_filter = ttk.Entry(
            header_filter_frame, textvariable=self.header_filter_var
        )
        self.entry_header_filter.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(6, 6))
        ttk.Button(
            header_filter_frame, text="Clear", command=self._clear_header_filter
        ).pack(side=tk.LEFT)
        self.entry_header_filter.bind("<KeyRelease>", self._on_header_filter_change)

        header_scope_frame = ttk.Frame(hdr_frame)
        header_scope_frame.pack(fill=tk.X, padx=6, pady=(4, 6))
        ttk.Label(header_scope_frame, text="Scope:").pack(side=tk.LEFT)
        self.header_scope_cb = ttk.Combobox(
            header_scope_frame,
            textvariable=self.header_scope_var,
            state="readonly",
            values=[
                "Dataset",
                "Shared Functional Groups",
                "Frame (current) — Combined",
                "Frame (current) — Per-frame only",
                "Frame (current) — Shared only",
            ],
        )
        self.header_scope_cb.current(0)
        self.header_scope_cb.pack(side=tk.LEFT, padx=(6, 6))
        self.header_scope_cb.bind("<<ComboboxSelected>>", self._on_header_scope_change)

        self.chk_link_header = ttk.Checkbutton(
            header_scope_frame,
            text="Auto-update with frame",
            variable=self.header_link_to_frame,
            command=self._on_header_scope_change,
        )
        self.chk_link_header.pack(side=tk.LEFT, padx=(6, 6))

        # Expand/collapse toggle
        self.chk_expand_all = ttk.Checkbutton(
            header_scope_frame,
            text="Expand all",
            variable=self.header_expand_all,
            command=self._on_expand_all_toggle,
        )
        self.chk_expand_all.pack(side=tk.LEFT)

        # Hierarchical tree
        columns = ("Tag", "Name", "Value")
        self.hdr_tree = ttk.Treeview(hdr_frame, columns=columns, show="tree headings")
        self.hdr_tree.heading("#0", text="Path")
        self.hdr_tree.column("#0", width=260, anchor="w")
        for col, width in zip(columns, (120, 200, 600)):
            self.hdr_tree.heading(col, text=col)
            self.hdr_tree.column(col, width=width, anchor="w")
        vsb = ttk.Scrollbar(hdr_frame, orient="vertical", command=self.hdr_tree.yview)
        self.hdr_tree.configure(yscrollcommand=vsb.set)
        self.hdr_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(6, 0), pady=6)
        vsb.pack(side=tk.RIGHT, fill=tk.Y, padx=(0, 6), pady=6)

        # Initial state and bindings for ROI button
        self._update_roi_button_appearance()
        self._on_toggle_roi_mode()

    def _bind_keys(self):
        self.bind("<Control-o>", lambda e: self.open_folder())
        self.bind("<Left>", lambda e: self.prev_file())
        self.bind("<Right>", lambda e: self.next_file())
        self.bind("<Up>", self._on_key_up)
        self.bind("<Down>", self._on_key_down)

        # ROI helpers
        self.bind("<Escape>", lambda e: self._cancel_roi_draw())
        self.bind("r", lambda e: self._clear_roi())

    def _show_about(self):
        msg = (
            f"{APP_NAME}\n"
            f"Version {APP_VERSION}\n\n"
            "A fast, pragmatic DICOM viewer with diffusion info overlays, "
            "multi-frame navigation, window/level control, and header exploration.\n\n"
            f"{APP_COPYRIGHT}"
        )
        messagebox.showinfo(title=f"About {APP_NAME}", message=msg)

    def _on_tree_select(self, event):
        sel = self.file_tree.selection()
        if not sel:
            return
        item_id = sel[0]
        path = self.tree_item_to_path.get(item_id)
        if not path:
            # Clicked on a non-leaf node (study or series)
            return
        # Update current_index consistent with filtered_files
        if path in self.filtered_files:
            self.current_index = self.filtered_files.index(path)
        self.load_file(path)

    # ----------------------------
    # Filters
    # ----------------------------

    def _clear_file_filter(self):
        self.file_filter_var.set("")
        self._apply_file_filter()

    def _on_file_filter_change(self, event=None):
        self._apply_file_filter()

    def _apply_file_filter(self):
        # With hierarchical Treeview, we rebuild the tree based on pattern
        if not self.folder:
            return
        self._populate_file_tree()

    def _clear_header_filter(self):
        self.header_filter_var.set("")
        self._rebuild_header_tree()

    def _on_header_filter_change(self, event=None):
        # Debounce: wait 150ms after last key press
        if self._header_filter_after_id is not None:
            self.after_cancel(self._header_filter_after_id)
        self._header_filter_after_id = self.after(150, self._rebuild_header_tree)

    def _on_header_scope_change(self, event=None):
        self._rebuild_header_tree()

    def _on_expand_all_toggle(self):
        self._set_tree_open_all(self.header_expand_all.get())

    def _set_tree_open_all(self, open_flag):
        def set_node(n):
            try:
                self.hdr_tree.item(n, open=open_flag)
            except Exception:
                pass
            for c in self.hdr_tree.get_children(n):
                set_node(c)

        for root in self.hdr_tree.get_children(""):
            set_node(root)

    def _on_file_expand_all_toggle(self):
        # Toggle all nodes in the left file tree based on the checkbox
        self._set_file_tree_open_all(self.file_expand_all.get())

    def _set_file_tree_open_all(self, open_flag):
        def set_node(n):
            try:
                self.file_tree.item(n, open=open_flag)
            except Exception:
                pass
            for c in self.file_tree.get_children(n):
                set_node(c)

        for root in self.file_tree.get_children(""):
            set_node(root)

    def _populate_file_tree(self):
        """
        Populate the Treeview from self.series_hierarchy and filter pattern.
        We treat only instance nodes as selectable files (leaf nodes).
        """
        # Save current selected path to reselect later if possible
        current_path = None
        if 0 <= self.current_index < len(self.filtered_files):
            current_path = self.filtered_files[self.current_index]

        # Clear tree and mapping
        for item in self.file_tree.get_children():
            self.file_tree.delete(item)
        self.tree_item_to_path.clear()

        if not self.series_hierarchy:
            return

        pattern = self.file_filter_var.get().strip().lower()

        def match_text(s):
            if pattern == "":
                return True
            return pattern in str(s).lower()

        # Build list of all file paths (for navigation)
        self.filtered_files = []

        # Add studies
        for study_uid, study_node in sorted(
            self.series_hierarchy.items(), key=lambda kv: kv[0]
        ):
            study_desc = study_node["study_desc"] or "Study"
            patient_name = study_node["patient_name"] or ""
            study_date = study_node["study_date"] or ""
            study_label = f"{study_desc}"
            info = f"Patient: {patient_name}  Date: {study_date}"

            open_flag = self.file_expand_all.get()

            # Study node insertion
            study_id = self.file_tree.insert(
                "",
                tk.END,
                text=study_label,
                values=(info,),
                open=open_flag,  # was open=True
            )

            # study_id = self.file_tree.insert(
            #     "",
            #     tk.END,
            #     text=study_label,
            #     values=(info,),
            #     open=True,
            # )

            added_any_series = False

            # Add series under study
            for series_uid, series_node in sorted(
                study_node["series"].items(),
                key=lambda kv: (
                    float(kv[1]["series_number"])
                    if kv[1]["series_number"] not in (None, "")
                    else 1e12
                ),
            ):
                sdesc = series_node["series_desc"] or "Series"
                snum = series_node["series_number"]
                series_label = (
                    f"Series {snum}: {sdesc}" if snum not in (None, "") else sdesc
                )
                series_info = f"{len(series_node['instances'])} instance(s)"

                # Series node insertion
                series_id = self.file_tree.insert(
                    study_id,
                    tk.END,
                    text=series_label,
                    values=(series_info,),
                    open=open_flag,  # was open=False
                )

                # series_id = self.file_tree.insert(
                #     study_id,
                #     tk.END,
                #     text=series_label,
                #     values=(series_info,),
                #     open=False,
                # )

                added_any_instance = False

                # Add instances under series
                for inst in series_node["instances"]:
                    path = inst["path"]
                    inst_num = inst["instance_number"]
                    fname = os.path.basename(path)
                    inst_label = (
                        f"Instance {inst_num}" if inst_num not in (None, "") else fname
                    )
                    inst_info = fname

                    relpath = (
                        os.path.relpath(path, self.folder) if self.folder else path
                    )
                    fields_to_match = " ".join(
                        [
                            study_label,
                            info,
                            series_label,
                            series_info,
                            inst_label,
                            inst_info,
                            relpath,
                        ]
                    )
                    if not match_text(fields_to_match):
                        continue

                    inst_id = self.file_tree.insert(
                        series_id,
                        tk.END,
                        text=inst_label,
                        values=(inst_info,),
                        open=False,
                    )
                    self.tree_item_to_path[inst_id] = path
                    self.filtered_files.append(path)
                    added_any_instance = True
                    added_any_series = True

                # If no instance remains after filtering, remove series
                if not added_any_instance:
                    self.file_tree.delete(series_id)

            # If no series remains under this study, remove study
            if not added_any_series:
                self.file_tree.delete(study_id)

        # Ensure expand/collapse state matches the checkbox after rebuild
        self._set_file_tree_open_all(self.file_expand_all.get())

        # Try to re-select the previously selected path
        if current_path and current_path in self.filtered_files:
            self.current_index = self.filtered_files.index(current_path)
            self._select_tree_item_by_path(current_path)
        elif self.filtered_files:
            self.current_index = 0
            self._select_tree_item_by_path(self.filtered_files[0])
        else:
            self.current_index = -1
            self.current_ds = None
            self.canvas.delete("all")
            for item in self.hdr_tree.get_children():
                self.hdr_tree.delete(item)

    def _select_tree_item_by_path(self, path):
        """Find the Treeview item that maps to path and select it."""
        for item_id, p in self.tree_item_to_path.items():
            if p == path:
                # Ensure all parents are opened
                parent = self.file_tree.parent(item_id)
                while parent:
                    self.file_tree.item(parent, open=True)
                    parent = self.file_tree.parent(parent)
                self.file_tree.selection_set(item_id)
                self.file_tree.see(item_id)
                self.load_file(path)
                break

    # ----------------------------
    # Folder and file handling
    # ----------------------------

    def open_folder(self):
        folder = filedialog.askdirectory(title="Select folder with DICOM files")
        if not folder:
            return
        self.load_folder(folder)

    def load_folder(self, folder):
        """Start loading a folder in a background thread."""
        self.folder = folder
        self.metadata_cache = {}
        self.pixel_cache = LRUCache(max_items=8)

        # Show initial status in the toolbar and force a repaint
        self.lbl_status.config(text=f"{folder} — scanning for DICOM files...")
        self.update_idletasks()

        # Disable Open button while working (optional)
        self.btn_open.config(state="disabled")

        def worker():
            # This runs in a background thread - NO direct Tk calls here!
            try:
                files = self._scan_dicom_files(folder)
                series_hierarchy, metadata_cache = self._build_series_hierarchy_thread(
                    files
                )
            except Exception as e:
                # Report error on main thread
                self.after(0, lambda: messagebox.showerror("Error", str(e)))
                files = []
                series_hierarchy = {}
                metadata_cache = {}

            # Hand results back to main thread
            self.after(
                0,
                lambda: self._finish_load_folder(
                    folder, files, series_hierarchy, metadata_cache
                ),
            )

        threading.Thread(target=worker, daemon=True).start()

    def _scan_dicom_files(self, folder):
        """
        Scan folder recursively and collect DICOM files.
        """
        out = []

        # First: build a flat list of candidate files
        candidates = []
        for root, _, files in os.walk(folder):
            for name in files:
                candidates.append(os.path.join(root, name))

        # Filter to probable DICOM files
        for path in candidates:
            name = os.path.basename(path)
            if name.lower().endswith((".dcm", ".dicom")):
                out.append(path)
                continue
            try:
                if is_dicom_file(path):
                    out.append(path)
            except Exception:
                pass

        out.sort()
        return out

    def _build_series_hierarchy_thread(self, files):
        """
        Build series_hierarchy and metadata_cache in a worker thread.
        No Tk calls here.
        """
        series_hierarchy = {}
        metadata_cache = {}

        for path in files:
            try:
                ds = pydicom.dcmread(path, stop_before_pixels=True, force=True)
                metadata_cache[path] = ds
            except Exception:
                continue

            study_uid = getattr(ds, "StudyInstanceUID", None) or "unknown-study"
            series_uid = getattr(ds, "SeriesInstanceUID", None) or "unknown-series"

            study_desc = getattr(ds, "StudyDescription", "") or ""
            patient_name = getattr(ds, "PatientName", "") or ""
            study_date = getattr(ds, "StudyDate", "") or ""

            series_desc = getattr(ds, "SeriesDescription", "") or ""
            series_num = getattr(ds, "SeriesNumber", None)
            instance_num = getattr(ds, "InstanceNumber", None)
            sop_uid = getattr(ds, "SOPInstanceUID", "")

            study_node = series_hierarchy.setdefault(
                study_uid,
                {
                    "study_desc": study_desc,
                    "patient_name": str(patient_name),
                    "study_date": study_date,
                    "series": {},
                },
            )

            series_node = study_node["series"].setdefault(
                series_uid,
                {
                    "series_desc": series_desc,
                    "series_number": series_num,
                    "instances": [],
                },
            )

            series_node["instances"].append(
                {
                    "path": path,
                    "instance_number": instance_num,
                    "sop_instance_uid": sop_uid,
                }
            )

        # Sort instances
        for _, study_node in series_hierarchy.items():
            for _, series_node in study_node["series"].items():
                series_node["instances"].sort(
                    key=lambda it: (
                        float(it["instance_number"])
                        if it["instance_number"] not in (None, "")
                        else 1e12
                    )
                )

        return series_hierarchy, metadata_cache

    def _finish_load_folder(self, folder, files, series_hierarchy, metadata_cache):
        """Called on the main thread when worker is done."""
        # Re-enable Open button
        self.btn_open.config(state="normal")

        # Update internal state
        self.files = files
        self.series_hierarchy = series_hierarchy
        self.metadata_cache = metadata_cache

        self.filtered_files = list(self.files)
        self.file_filter_var.set("")

        # Clear old Treeview
        if hasattr(self, "file_tree"):
            for item in self.file_tree.get_children():
                self.file_tree.delete(item)
            self.tree_item_to_path.clear()

        count = len(self.files)

        # FINAL status text – this WILL be visible now
        self.lbl_status.config(text=f"{folder} — {count} DICOM file(s) loaded")
        self.update_idletasks()

        self.current_index = -1
        self.current_ds = None
        self.canvas.delete("all")
        for item in self.hdr_tree.get_children():
            self.hdr_tree.delete(item)
        self._update_frame_controls(1, 1)

        # Populate Treeview (also selects first instance if any)
        self._populate_file_tree()

        if not self.filtered_files:
            messagebox.showinfo(
                "No DICOM files", "No DICOM files found in the selected folder."
            )

    def _build_series_hierarchy(self):
        """
        Scan self.files and build hierarchical structure:
        Study (StudyInstanceUID/StudyDescription) →
            Series (SeriesInstanceUID/SeriesNumber/SeriesDescription) →
                Instances (InstanceNumber, file path).
        """
        self.series_hierarchy = {}

        for path in self.files:
            try:
                # Use cache if available
                ds = self.metadata_cache.get(path)
                if ds is None:
                    ds = pydicom.dcmread(path, stop_before_pixels=True, force=True)
                    self.metadata_cache[path] = ds
            except Exception:
                continue

            study_uid = getattr(ds, "StudyInstanceUID", None) or "unknown-study"
            series_uid = getattr(ds, "SeriesInstanceUID", None) or "unknown-series"

            study_desc = getattr(ds, "StudyDescription", "") or ""
            patient_name = getattr(ds, "PatientName", "") or ""
            study_date = getattr(ds, "StudyDate", "") or ""

            series_desc = getattr(ds, "SeriesDescription", "") or ""
            series_num = getattr(ds, "SeriesNumber", None)
            instance_num = getattr(ds, "InstanceNumber", None)
            sop_uid = getattr(ds, "SOPInstanceUID", "")

            # Init study node
            study_node = self.series_hierarchy.setdefault(
                study_uid,
                {
                    "study_desc": study_desc,
                    "patient_name": str(patient_name),
                    "study_date": study_date,
                    "series": {},
                },
            )

            # Init series node
            series_node = study_node["series"].setdefault(
                series_uid,
                {
                    "series_desc": series_desc,
                    "series_number": series_num,
                    "instances": [],
                },
            )

            series_node["instances"].append(
                {
                    "path": path,
                    "instance_number": instance_num,
                    "sop_instance_uid": sop_uid,
                }
            )

        # Sort instances by InstanceNumber (if available)
        for study_uid, study_node in self.series_hierarchy.items():
            for series_uid, series_node in study_node["series"].items():
                series_node["instances"].sort(
                    key=lambda it: (
                        float(it["instance_number"])
                        if it["instance_number"] not in (None, "")
                        else 1e12
                    )
                )

    # ----------------------------
    # Selection and navigation
    # ----------------------------

    def select_index(self, idx):
        if idx < 0 or idx >= len(self.filtered_files):
            return
        self.current_index = idx
        path = self.filtered_files[idx]
        self._select_tree_item_by_path(path)

    def next_file(self):
        if self.current_index < len(self.filtered_files) - 1:
            self.select_index(self.current_index + 1)

    def prev_file(self):
        if self.current_index > 0:
            self.select_index(self.current_index - 1)

    def _on_key_up(self, event):
        if self.current_index > 0:
            self.select_index(self.current_index - 1)

    def _on_key_down(self, event):
        if self.current_index < len(self.filtered_files) - 1:
            self.select_index(self.current_index + 1)

    # ----------------------------
    # Frame navigation (multi-frame) via buttons
    # ----------------------------

    def _update_frame_controls(self, current, total):
        current = int(current)
        total = int(total)
        # Update label
        self.frame_label.config(text=f"Frame {current}/{total}")

        # Update slider range and value
        if self.frame_slider is not None:
            # Temporarily disable callback while programmatically setting the value
            self.frame_slider.configure(from_=1, to=max(1, total))
            # Avoid recursion by suspending command, then restoring
            cmd = self.frame_slider.cget("command")
            self.frame_slider.configure(command=None)
            try:
                self.frame_slider.set(current)
            finally:
                self.frame_slider.configure(command=cmd)

        # Enable/disable buttons
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
        if not self.current_ds:
            return
        total = int(getattr(self.current_ds, "NumberOfFrames", 1))
        if total <= 1:
            return
        if self.current_frame_index < total - 1:
            self.current_frame_index += 1
        # Clear ROI when changing frame
        self._clear_roi()
        self._update_frame_controls(self.current_frame_index + 1, total)
        self._render_image()
        if self.header_link_to_frame.get() and self.header_scope_var.get().startswith(
            "Frame"
        ):
            self._rebuild_header_tree()

    def prev_frame(self):
        if not self.current_ds:
            return
        total = int(getattr(self.current_ds, "NumberOfFrames", 1))
        if total <= 1:
            return
        if self.current_frame_index > 0:
            self.current_frame_index -= 1
        # Clear ROI when changing frame
        self._clear_roi()
        self._update_frame_controls(self.current_frame_index + 1, total)
        self._render_image()
        if self.header_link_to_frame.get() and self.header_scope_var.get().startswith(
            "Frame"
        ):
            self._rebuild_header_tree()

    def _on_frame_slider_change(self, value):
        """
        Called when user drags the frame slider.
        Value is a float; convert to int frame index (0-based).
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

        # Clamp to [1, total]
        frame_num = max(1, min(total, frame_num))
        new_index = frame_num - 1

        if new_index == self.current_frame_index:
            return

        # Clear ROI when changing frame
        self._clear_roi()

        self.current_frame_index = new_index
        self._update_frame_controls(self.current_frame_index + 1, total)
        self._render_image()

        if self.header_link_to_frame.get() and self.header_scope_var.get().startswith(
            "Frame"
        ):
            self._rebuild_header_tree()

    # Mouse wheel can still change frames (optional)
    def _on_mouse_wheel(self, event):
        self._interactive_resize = True
        self._update_canvas_image()
        # You can schedule a “relax” high-quality redraw shortly after:
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

    # ----------------------------
    # Mouse wheel (zoom) & pan
    # ----------------------------

    def _on_mouse_wheel_zoom(self, event):
        self._interactive_resize = True
        self._update_canvas_image()
        # You can schedule a “relax” high-quality redraw shortly after:
        self.after(100, self._finish_interactive_zoom)

        if self.current_image_pil is None:
            return

        if hasattr(event, "delta") and event.delta != 0:
            direction = 1 if event.delta > 0 else -1
        elif hasattr(event, "num"):
            if event.num == 4:
                direction = 1
            elif event.num == 5:
                direction = -1
            else:
                direction = 0
        else:
            direction = 0

        if direction == 0:
            return

        old_zoom = self.zoom
        factor = 1.1
        if direction > 0:
            self.zoom *= factor
        else:
            self.zoom /= factor

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
        self._interactive_resize = False
        self._update_canvas_image()  # final high-quality redraw

    def _on_pan_start(self, event):
        self._interactive_resize = True
        if self.current_image_pil is None:
            return
        self._drag_start_x = event.x
        self._drag_start_y = event.y
        self._drag_start_pan_x = self.pan_x
        self._drag_start_pan_y = self.pan_y

    def _on_pan_move(self, event):
        if self._drag_start_x is None or self.current_image_pil is None:
            return

        dx = event.x - self._drag_start_x
        dy = event.y - self._drag_start_y

        self.pan_x = self._drag_start_pan_x + dx
        self.pan_y = self._drag_start_pan_y + dy

        self._update_canvas_image()

    def _on_pan_end(self, event):
        self._interactive_resize = False
        self._update_canvas_image()  # final high-quality redraw
        self._drag_start_x = None
        self._drag_start_y = None
        self._drag_start_pan_x = None
        self._drag_start_pan_y = None

    def _on_reset_zoom_pan(self, event):
        self.zoom = 1.0
        self.pan_x = 0.0
        self.pan_y = 0.0
        self._update_canvas_image()

    # ----------------------------
    # Canvas/image mapping & drawing
    # ----------------------------

    def _effective_zoom(self):
        """Effective zoom in canvas pixels per image pixel, combining fit-to-window and user zoom."""
        if self.current_image_pil is None:
            return 1.0
        canvas_w = max(1, self.canvas.winfo_width())
        canvas_h = max(1, self.canvas.winfo_height())
        base_w, base_h = self.current_image_pil.size
        scale_to_fit = min(canvas_w / base_w, canvas_h / base_h)
        return max(0.0001, scale_to_fit * max(self.zoom, 0.1))

    def _get_image_bbox_on_canvas(self):
        """Return (x0, y0, x1, y1, z_eff) of the image on the canvas and effective zoom."""
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
        cx = canvas_w // 2
        cy = canvas_h // 2
        img_cx = cx + self.pan_x
        img_cy = cy + self.pan_y

        x0 = img_cx - zoomed_w / 2.0
        y0 = img_cy - zoomed_h / 2.0
        x1 = x0 + zoomed_w
        y1 = y0 + zoomed_h
        return (x0, y0, x1, y1, z)

    def _canvas_to_image_coords(self, x, y):
        """Convert canvas coords to image pixel (i, j). Return None if outside."""
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
        return i, j

    def _update_canvas_image(self):
        self.canvas.delete("all")
        if self.current_image_pil is None:
            return

        base_w, base_h = self.current_image_pil.size
        z = self._effective_zoom()

        zoomed_w = int(base_w * z)
        zoomed_h = int(base_h * z)
        if zoomed_w < 1 or zoomed_h < 1:
            return

        # Choose resampling based on interaction state
        resample_method = (
            PILImage.BILINEAR if self._interactive_resize else PILImage.LANCZOS
        )
        # Single resize using the intended method
        zoomed_img = self.current_image_pil.resize(
            (zoomed_w, zoomed_h), resample_method
        )
        self.current_image_tk = ImageTk.PhotoImage(zoomed_img)

        canvas_w = max(1, self.canvas.winfo_width())
        canvas_h = max(1, self.canvas.winfo_height())
        cx = canvas_w // 2
        cy = canvas_h // 2

        x = cx + self.pan_x
        y = cy + self.pan_y

        self.canvas.create_image(x, y, image=self.current_image_tk, anchor="center")

        # Draw overlays after image so they appear on top
        self._draw_overlay()  # crosshair + pixel readout
        self._redraw_roi_overlay()  # freehand ROI overlay
        self._draw_diffusion_overlay()
        self._draw_basic_metadata_overlay()

    # ----------------------------
    # Loading and displaying DICOM
    # ----------------------------

    def load_file(self, path):
        # Try use cached metadata first
        ds = self.metadata_cache.get(path)
        try:
            if ds is None:
                ds = pydicom.dcmread(path, force=True)
                self.metadata_cache[path] = ds
            else:
                # We cached without pixels; if pixels missing, read full dataset
                if "PixelData" not in ds:
                    ds = pydicom.dcmread(path, force=True)
                    self.metadata_cache[path] = ds
        except Exception as e:
            messagebox.showerror(
                "Read Error", f"Failed to read DICOM file:\n{path}\n\n{e}"
            )
            return

        self.current_ds = ds
        self.current_frame_index = 0

        # Cache pixel_array once per file (LRU)
        if path in self.pixel_cache:
            ds._cached_pixel_array = self.pixel_cache[path]
        else:
            try:
                arr = ds.pixel_array  # triggers decode once
                self.pixel_cache[path] = arr
                ds._cached_pixel_array = arr
            except Exception:
                ds._cached_pixel_array = None

        # Clear ROI for new file
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
            text=f"[{self.current_index+1}/{len(self.filtered_files)}] {os.path.basename(path)} — "
            f"Patient: {getattr(ds, 'PatientName', 'N/A')} — "
            f"Study: {getattr(ds, 'StudyDescription', 'N/A')}"
        )

        self._render_image()
        self._rebuild_header_tree()

    def _sync_wl_controls(self):
        if self.window_center is None:
            c = self._default_window_center or 40.0
        else:
            c = self.window_center

        if self.window_width is None:
            w = self._default_window_width or 400.0
        else:
            w = self.window_width

        self.level_slider.configure(from_=c - 2000, to=c + 2000)
        self.window_slider.configure(from_=1, to=max(100, w * 4))
        self.level_slider.set(c)
        self.window_slider.set(w)

    def _init_default_window_level(self, ds):
        wc = getattr(ds, "WindowCenter", None)
        ww = getattr(ds, "WindowWidth", None)

        from pydicom.multival import MultiValue

        if isinstance(wc, MultiValue) or isinstance(wc, (list, tuple)):
            wc = wc[0]
        if isinstance(ww, MultiValue) or isinstance(ww, (list, tuple)):
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
            c = (amin + amax) / 2.0
            w = amax - amin
            self._default_window_center = c
            self._default_window_width = max(1.0, w)
        except Exception:
            self._default_window_center = 40.0
            self._default_window_width = 400.0

    def _render_image(self):
        if not self.current_ds:
            return

        # Cache frame arrays for readout
        try:
            arr = getattr(self.current_ds, "_cached_pixel_array", None)
            if arr is None:
                arr = self.current_ds.pixel_array

            if arr.ndim >= 3 and getattr(self.current_ds, "NumberOfFrames", 1) > 1:
                frame = arr[self.current_frame_index]
            else:
                frame = arr

            self._frame_is_color = getattr(self.current_ds, "SamplesPerPixel", 1) == 3
            if self._frame_is_color:
                self._frame_raw = frame
                self._frame_modality = None
            else:
                self._frame_raw = frame
                try:
                    self._frame_modality = apply_modality_lut(frame, self.current_ds)
                except Exception:
                    self._frame_modality = None
        except Exception:
            self._frame_raw = None
            self._frame_modality = None
            self._frame_is_color = False

        # Render PIL image for display
        try:
            pil_img = dicom_to_display_image(
                self.current_ds,
                frame_index=self.current_frame_index,
                window_center=self.window_center,
                window_width=self.window_width,
            )
        except Exception as e:
            self.current_image_pil = None
            self.current_image_tk = None
            self.canvas.delete("all")
            w = max(100, self.canvas.winfo_width())
            h = max(100, self.canvas.winfo_height())
            self.canvas.create_text(
                w // 2, h // 2, text=f"Image render error:\n{e}", fill="white"
            )
            return

        self.current_image_pil = pil_img
        self._update_canvas_image()

    # ----------------------------
    # Header (nested) building and filtering
    # ----------------------------

    def _rebuild_header_tree(self):
        # Clear tree
        for item in self.hdr_tree.get_children():
            self.hdr_tree.delete(item)

        if not self.current_ds:
            return

        pattern = self.header_filter_var.get().strip().lower()
        scope = self.header_scope_var.get()
        open_flag = self.header_expand_all.get()

        if scope == "Dataset":
            root = self.hdr_tree.insert(
                "", tk.END, text="Dataset", values=("", "", ""), open=open_flag
            )
            self._insert_summary(root, pattern, open_flag)
            self._insert_dataset_recursive(root, self.current_ds, pattern, open_flag)

        elif scope == "Shared Functional Groups":
            if (
                hasattr(self.current_ds, "SharedFunctionalGroupsSequence")
                and len(self.current_ds.SharedFunctionalGroupsSequence) > 0
            ):
                root = self.hdr_tree.insert(
                    "",
                    tk.END,
                    text="Shared Functional Groups",
                    values=("", "", ""),
                    open=open_flag,
                )
                for idx, item in enumerate(
                    self.current_ds.SharedFunctionalGroupsSequence, start=1
                ):
                    item_id = self.hdr_tree.insert(
                        root,
                        tk.END,
                        text=f"Item {idx}",
                        values=("", "", ""),
                        open=open_flag,
                    )
                    self._insert_dataset_recursive(item_id, item, pattern, open_flag)
                if not self.hdr_tree.get_children(root):
                    self.hdr_tree.delete(root)
            else:
                self.hdr_tree.insert(
                    "",
                    tk.END,
                    text="Shared Functional Groups: (absent)",
                    values=("", "", ""),
                    open=open_flag,
                )

        else:
            # Frame scopes
            frame_num = self.current_frame_index + 1
            per_frame = getattr(
                self.current_ds, "PerFrameFunctionalGroupsSequence", None
            )
            shared = getattr(self.current_ds, "SharedFunctionalGroupsSequence", None)
            frame_root = self.hdr_tree.insert(
                "",
                tk.END,
                text=f"Frame {frame_num}",
                values=("", "", ""),
                open=open_flag,
            )

            if scope.endswith("Per-frame only") or scope.endswith("Combined"):
                if per_frame is not None and len(per_frame) > self.current_frame_index:
                    pf_item = per_frame[self.current_frame_index]
                    pf_root = self.hdr_tree.insert(
                        frame_root,
                        tk.END,
                        text="PerFrame Functional Groups",
                        values=("", "", ""),
                        open=open_flag,
                    )
                    self._insert_dataset_recursive(pf_root, pf_item, pattern, open_flag)
                    if not self.hdr_tree.get_children(pf_root):
                        self.hdr_tree.delete(pf_root)
                else:
                    if scope.endswith("Per-frame only"):
                        self.hdr_tree.insert(
                            frame_root,
                            tk.END,
                            text="PerFrame Functional Groups: (absent)",
                            values=("", "", ""),
                            open=open_flag,
                        )

            if scope.endswith("Shared only") or scope.endswith("Combined"):
                if shared is not None and len(shared) > 0:
                    sh_root = self.hdr_tree.insert(
                        frame_root,
                        tk.END,
                        text="Shared Functional Groups",
                        values=("", "", ""),
                        open=open_flag,
                    )
                    for idx, item in enumerate(shared, start=1):
                        item_id = self.hdr_tree.insert(
                            sh_root,
                            tk.END,
                            text=f"Item {idx}",
                            values=("", "", ""),
                            open=open_flag,
                        )
                        self._insert_dataset_recursive(
                            item_id, item, pattern, open_flag
                        )
                    if not self.hdr_tree.get_children(sh_root):
                        self.hdr_tree.delete(sh_root)
                else:
                    if scope.endswith("Shared only"):
                        self.hdr_tree.insert(
                            frame_root,
                            tk.END,
                            text="Shared Functional Groups: (absent)",
                            values=("", "", ""),
                            open=open_flag,
                        )

            if not self.hdr_tree.get_children(frame_root):
                self.hdr_tree.delete(frame_root)

        # Apply expand/collapse state to current tree
        self._set_tree_open_all(self.header_expand_all.get())

    def _insert_summary(self, root, pattern, open_flag):
        summary = [
            ("(0010,0010)", "PatientName", getattr(self.current_ds, "PatientName", "")),
            ("(0010,0020)", "PatientID", getattr(self.current_ds, "PatientID", "")),
            ("(0008,0020)", "StudyDate", getattr(self.current_ds, "StudyDate", "")),
            ("(0008,0060)", "Modality", getattr(self.current_ds, "Modality", "")),
            (
                "(0008,1030)",
                "StudyDescription",
                getattr(self.current_ds, "StudyDescription", ""),
            ),
            (
                "(0008,103E)",
                "SeriesDescription",
                getattr(self.current_ds, "SeriesDescription", ""),
            ),
            (
                "(0020,0011)",
                "SeriesNumber",
                getattr(self.current_ds, "SeriesNumber", ""),
            ),
            (
                "(0020,0013)",
                "InstanceNumber",
                getattr(self.current_ds, "InstanceNumber", ""),
            ),
        ]
        matched_any = False
        sum_root = self.hdr_tree.insert(
            root, tk.END, text="Summary", values=("", "", ""), open=open_flag
        )
        for tag_str, name, val in summary:
            tag_str_s = tag_str
            name_s = name
            val_s = safe_str(val)
            if self._matches(pattern, [tag_str_s, name_s, val_s, "Summary"]):
                self.hdr_tree.insert(
                    sum_root,
                    tk.END,
                    text=name_s,
                    values=(tag_str_s, name_s, val_s),
                    open=open_flag,
                )
                matched_any = True
        if not matched_any:
            self.hdr_tree.delete(sum_root)

    def _insert_dataset_recursive(self, parent_id, dataset, pattern, open_flag):
        """Recursively insert dataset elements. Prune nodes that do not match filter."""
        inserted_any = False
        for elem in dataset:
            if elem.VR == "SQ":
                seq_name = elem.keyword or elem.name or "Sequence"
                tag_str = format_tag(elem.tag)
                seq_matches_self = self._matches(
                    pattern,
                    [
                        tag_str,
                        seq_name,
                        f"Sequence ({len(elem.value)} item(s))",
                        seq_name,
                    ],
                )
                seq_id = self.hdr_tree.insert(
                    parent_id,
                    tk.END,
                    text=seq_name,
                    values=(tag_str, seq_name, f"Sequence ({len(elem.value)} item(s))"),
                    open=open_flag,
                )
                inserted_children = False
                try:
                    for idx, item in enumerate(elem.value, start=1):
                        item_id = self.hdr_tree.insert(
                            seq_id,
                            tk.END,
                            text=f"Item {idx}",
                            values=("", "", ""),
                            open=open_flag,
                        )
                        child_inserted = self._insert_dataset_recursive(
                            item_id, item, pattern, open_flag
                        )
                        if not child_inserted:
                            self.hdr_tree.delete(item_id)
                        else:
                            inserted_children = True
                except Exception:
                    pass

                if pattern == "" or seq_matches_self or inserted_children:
                    inserted_any = True
                else:
                    self.hdr_tree.delete(seq_id)

            else:
                tag_str = format_tag(elem.tag)
                name = elem.keyword or elem.name or ""
                val_preview = safe_str(elem.value)
                if self._matches(pattern, [tag_str, name, val_preview, name]):
                    self.hdr_tree.insert(
                        parent_id,
                        tk.END,
                        text=name or tag_str,
                        values=(tag_str, name, val_preview),
                        open=open_flag,
                    )
                    inserted_any = True
        return inserted_any

    def _matches(self, pattern, fields):
        if pattern == "":
            return True
        try:
            for f in fields:
                if f is None:
                    continue
                if pattern in str(f).lower():
                    return True
        except Exception:
            pass
        return False

    # ----------------------------
    # Diffusion overlay
    # ----------------------------

    def _get_diffusion_info_for_current_frame(self):
        """Extract diffusion info (b-value, directionality, gradient vector) for current frame."""
        ds = self.current_ds
        if ds is None:
            return None

        # Try per-frame first
        per_frame = getattr(ds, "PerFrameFunctionalGroupsSequence", None)
        mr = None
        if per_frame is not None and len(per_frame) > self.current_frame_index:
            pf_item = per_frame[self.current_frame_index]
            mr_seq = getattr(pf_item, "MRDiffusionSequence", None)
            if mr_seq and len(mr_seq) > 0:
                mr = mr_seq[0]

        # Fallback to shared
        if mr is None:
            shared = getattr(ds, "SharedFunctionalGroupsSequence", None)
            if shared and len(shared) > 0:
                for item in shared:
                    mr_seq = getattr(item, "MRDiffusionSequence", None)
                    if mr_seq and len(mr_seq) > 0:
                        mr = mr_seq[0]
                        break

        if mr is None:
            return None

        def get_attr(d, name, tag_hex=None):
            val = getattr(d, name, None)
            if val is not None:
                return val
            if tag_hex is not None:
                try:
                    tag = Tag(tag_hex)
                    elem = d.get(tag, None)
                    if elem is not None:
                        return elem.value
                except Exception:
                    pass
            return None

        bval = get_attr(mr, "DiffusionBValue", 0x00189087)
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
                    try:
                        grad_vec = tuple(
                            float(x)
                            for x in (vec if isinstance(vec, (list, tuple)) else [vec])
                        )
                        if len(grad_vec) == 1:
                            grad_vec = None
                        elif len(grad_vec) > 3:
                            grad_vec = grad_vec[:3]
                    except Exception:
                        grad_vec = None
            except Exception:
                grad_vec = None
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
                    grad_vec = None

        b_str = (
            f"{bval:.1f}"
            if isinstance(bval, (int, float))
            else (str(bval) if bval is not None else "n/a")
        )
        d_str = str(dirality) if dirality is not None else "n/a"
        g_str = "n/a"
        if grad_vec is not None and len(grad_vec) == 3:
            g_str = f"[{grad_vec[0]:.3f}, {grad_vec[1]:.3f}, {grad_vec[2]:.3f}]"

        return {"b": b_str, "dir": d_str, "grad": g_str}

    def _draw_diffusion_overlay(self):
        """Draw diffusion info text at the bottom-left of the displayed image."""
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
        # Bottom-left inside the image bbox
        tx = max(5, int(x0) + pad)
        ty = int(y1) - pad

        # text = f"Diffusion: b={info['b']} | Dir={info['dir']} | Grad={info['grad']}"
        text = f"Diffusion: b={info['b']} | Dir={info['dir']}"
        txt_id = self.canvas.create_text(
            tx,
            ty,
            text=text,
            fill="#00e0ff",
            anchor="sw",  # bottom-left anchor
            font=("TkDefaultFont", 10, "bold"),
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

    # ----------------------------
    # Basic DICOM metadata overlay (right-aligned vertical stack)
    # ----------------------------

    def _get_basic_metadata_lines(self):
        ds = self.current_ds
        if ds is None:
            return []
        # Compose common metadata lines; use safe_str for readability
        lines = []
        lines.append(f"Image type: {safe_str(getattr(ds, 'ImageType', '')) or 'N/A'}")
        lines.append(f"Study Date: {safe_str(getattr(ds, 'StudyDate', '')) or 'N/A'}")
        lines.append(
            f"Series: {safe_str(getattr(ds, 'SeriesDescription', '')) or 'N/A'}"
        )
        lines.append(f"Series No: {safe_str(getattr(ds, 'SeriesNumber', '')) or 'N/A'}")
        lines.append(
            f"Instance: {safe_str(getattr(ds, 'InstanceNumber', '')) or 'N/A'}"
        )
        lines.append(
            f"matrix: {safe_str(getattr(ds, 'Rows', '')) or 'N/A'} x {safe_str(getattr(ds, 'Columns', '')) or 'N/A'}"
        )
        # Frame info
        total_frames = int(getattr(ds, "NumberOfFrames", 1))
        lines.append(f"Frame: {self.current_frame_index + 1}/{total_frames}")
        return lines

    def _draw_basic_metadata_overlay(self):
        """Draw basic DICOM metadata at the top-right of the displayed image, stacked vertically, right-aligned."""
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
        xR = int(x1) - pad  # right margin inside the image bbox
        y = max(5, int(y0) + pad)

        # Draw each line right-aligned with a subtle background
        for line in lines:
            txt_id = self.canvas.create_text(
                xR,
                y,
                text=line,
                fill="#ffffff",
                anchor="ne",
                font=("TkDefaultFont", 10),
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
                # Ensure the text stays above the rectangle
                self.canvas.tag_lower(rect_id, txt_id)
                # Advance y to just below the rectangle
                y = yB + line_gap
            except Exception:
                # Fallback: increment y by font height approx
                y += 16 + line_gap

    # ----------------------------
    # Header end
    # ----------------------------

    def _on_resize(self, event):
        if not self.current_ds:
            return
        self._update_canvas_image()

    def _on_window_change(self, value):
        try:
            self.window_width = float(value)
        except Exception:
            return
        if self.window_width <= 0:
            self.window_width = 1.0
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
            else self._default_window_center
        )
        self._wl_start_width = (
            self.window_width
            if self.window_width is not None
            else self._default_window_width
        )
        if self._wl_start_width is None or self._wl_start_width <= 0:
            self._wl_start_width = 400.0
        if self._wl_start_center is None:
            self._wl_start_center = 40.0

    def _on_wl_move(self, event):
        if self._wl_drag_start_x is None:
            return

        dx = event.x - self._wl_drag_start_x
        dy = event.y - self._wl_drag_start_y

        w = self._wl_start_width + dx * 2.0
        c = self._wl_start_center - dy * 2.0

        if w <= 1:
            w = 1.0

        self.window_width = w
        self.window_center = c

        self._sync_wl_controls()
        self._render_image()

    def _on_wl_end(self, event):
        self._wl_drag_start_x = None
        self._wl_drag_start_y = None
        self._wl_start_center = None
        self._wl_start_width = None

    # ----------------------------
    # Crosshair overlay & pixel readout
    # ----------------------------

    def _draw_overlay(self):
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
            font=("TkDefaultFont", 9),
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
        result = {
            "SV": "-",
            "label": f"x {i} y {j} | SV=-",
        }

        try:
            dv = self.current_image_pil.getpixel((i, j))
            if isinstance(dv, tuple):
                result["DV"] = f"RGB{dv}"
            else:
                result["DV"] = int(dv)
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
                    if getattr(self.current_ds, "Modality", "").upper() == "CT":
                        result["MV"] = f"{int(round(mv))} HU"
                    else:
                        result["MV"] = f"{mv:.2f}"
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
        if i is None or j is None or vals is None:
            self.lbl_cursor.config(text="Cursor: x=- y=- | SV=-")
        else:
            self.lbl_cursor.config(text=f"Cursor: x {i} y {j} | SV={vals['SV']}")

    def _on_mouse_move(self, event):
        if self.current_image_pil is None:
            return
        now = self.winfo_toplevel().tk.call("clock", "milliseconds")
        try:
            now = int(now)
        except Exception:
            now = 0

        # Throttle to ~50 FPS (20 ms)
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
        self._mouse_x_canvas = None
        self._mouse_y_canvas = None
        self.canvas.delete("overlay")
        self._update_cursor_label(None, None, None)

    # ----------------------------
    # Freehand ROI: button toggle, draw, mask, stats, overlay
    # ----------------------------

    def _toggle_roi_button(self):
        # Toggle the BooleanVar and apply bindings
        self.roi_mode.set(not self.roi_mode.get())
        self._update_roi_button_appearance()
        self._on_toggle_roi_mode()

    def _update_roi_button_appearance(self):
        if self.roi_mode.get():
            self.btn_roi_toggle.config(text="Exit ROI")
        else:
            self.btn_roi_toggle.config(text="Draw ROI")

    def _on_toggle_roi_mode(self):
        # Cancel in-progress drawing when toggling
        self._cancel_roi_draw()
        if self.roi_mode.get():
            # ROI mode ON -> bind drawing to LMB
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
            # ROI mode OFF -> restore pan on LMB
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

    def _on_frame_slider_wheel(self, event):
        if not self.current_ds:
            return
        total = int(getattr(self.current_ds, "NumberOfFrames", 1))
        if total <= 1:
            return

        # Direction
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
            # Reuse existing mechanism
            self.current_frame_index = new_index
            self._clear_roi()
            self._update_frame_controls(self.current_frame_index + 1, total)
            self._render_image()
            if (
                self.header_link_to_frame.get()
                and self.header_scope_var.get().startswith("Frame")
            ):
                self._rebuild_header_tree()

    def _update_roi_label(self):
        if not self.roi_stats:
            self.lbl_roi.config(text="ROI: N=0 μ=- σ=- med=- IQR=-")
            return
        s = self.roi_stats
        self.lbl_roi.config(
            text=f"ROI: N={s['N']} μ={s['mean']:.2f} σ={s['std']:.2f} med={s['median']:.2f} IQR={s['iqr']:.2f}"
        )

    def _cancel_roi_draw(self):
        if self._roi_drawing:
            self._roi_drawing = False
            self.roi_points = []
            self._redraw_roi_overlay()

    def _clear_roi(self):
        self._roi_drawing = False
        self.roi_points = []
        self.roi_mask = None
        self.roi_stats = None
        self._redraw_roi_overlay()
        self._update_roi_label()

    def _on_roi_start(self, event):
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
        if not self._roi_drawing or self.current_image_pil is None:
            return
        ij = self._canvas_to_image_coords(event.x, event.y)
        if ij is None:
            return
        if (not self.roi_points) or (
            abs(ij[0] - self.roi_points[-1][0]) >= 1
            or abs(ij[1] - self.roi_points[-1][1]) >= 1
        ):
            self.roi_points.append(ij)
            self._redraw_roi_overlay()

    def _on_roi_end(self, event):
        if not self._roi_drawing:
            return
        self._roi_drawing = False
        if len(self.roi_points) >= 3:
            self._finalize_roi()
        else:
            self.roi_points = []
            self._redraw_roi_overlay()

    def _finalize_roi(self):
        # Build a mask in image coordinates (rows=H, cols=W)
        W, H = self.current_image_pil.size
        if len(self.roi_points) < 3:
            self.roi_mask = None
            self.roi_stats = None
            self._redraw_roi_overlay()
            self._update_roi_label()
            return

        try:
            from PIL import ImageDraw

            mask_img = PILImage.new("L", (W, H), 0)
            draw = ImageDraw.Draw(mask_img)
            poly = [(int(x), int(y)) for (x, y) in self.roi_points]
            draw.polygon(poly, fill=1, outline=1)
            mask = np.array(mask_img, dtype=bool)
            self.roi_mask = mask
        except Exception:
            self.roi_mask = None

        self.roi_stats = self._compute_roi_stats()
        self._redraw_roi_overlay()
        self._update_roi_label()

    def _get_scalar_frame_for_stats(self):
        """
        Returns a 2D numpy array with scalar values to compute stats on:
        - If available, modality LUT-applied grayscale (e.g., HU).
        - Else raw frame.
        - If color, returns luminance: 0.2126 R + 0.7152 G + 0.0722 B
        """
        if self._frame_is_color and self._frame_raw is not None:
            arr = self._frame_raw.astype(np.float32)
            if arr.ndim == 3 and arr.shape[2] >= 3:
                R = arr[..., 0]
                G = arr[..., 1]
                B = arr[..., 2]
                return 0.2126 * R + 0.7152 * G + 0.0722 * B
            return arr.mean(axis=-1) if arr.ndim == 3 else arr.astype(np.float32)

        if self._frame_modality is not None:
            return self._frame_modality.astype(np.float32)
        if self._frame_raw is not None:
            return self._frame_raw.astype(np.float32)
        return None

    def _compute_roi_stats(self):
        """
        Compute mean, std (population), median, IQR on ROI pixels.
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
            mean = float(np.mean(roi_vals))
            std = float(np.std(roi_vals))  # population std
            q1, med, q3 = np.percentile(roi_vals, [25, 50, 75])
            iqr = float(q3 - q1)
            return {"N": N, "mean": mean, "std": std, "median": float(med), "iqr": iqr}
        except Exception:
            return None

    def _image_to_canvas_points(self, pts):
        """
        Convert list of (i,j) image coords to canvas coords using current bbox and zoom.
        """
        bbox = self._get_image_bbox_on_canvas()
        if bbox is None:
            return []
        x0, y0, x1, y1, z = bbox
        out = []
        for i, j in pts:
            cx = x0 + (i + 0.5) * z
            cy = y0 + (j + 0.5) * z
            out.append((cx, cy))
        return out

    def _redraw_roi_overlay(self):
        # Remove existing ROI items
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

        # While drawing: show polyline
        if self._roi_drawing:
            for k in range(1, len(pts_canvas)):
                x0, y0 = pts_canvas[k - 1]
                x1, y1 = pts_canvas[k]
                self._roi_items.append(
                    self.canvas.create_line(
                        x0, y0, x1, y1, fill="#ff8800", width=2, tags=("roi",)
                    )
                )
            return

        # Finalized: filled polygon with outline
        flat = [c for xy in pts_canvas for c in xy]
        try:
            poly_id = self.canvas.create_polygon(
                *flat,
                fill="#ffff00",  # color; we simulate translucency with stipple below
                outline="#ff8800",
                width=2,
                tags=("roi",),
            )
            # Simulate translucency with stipple if available
            try:
                self.canvas.itemconfigure(poly_id, stipple="gray25")
            except Exception:
                pass
            self._roi_items.append(poly_id)
        except Exception:
            # Fallback: draw closed polyline
            for k in range(len(pts_canvas)):
                x0, y0 = pts_canvas[k]
                x1, y1 = pts_canvas[(k + 1) % len(pts_canvas)]
                self._roi_items.append(
                    self.canvas.create_line(
                        x0, y0, x1, y1, fill="#ff8800", width=2, tags=("roi",)
                    )
                )

        # Optional: place a small stats box at polygon centroid
        try:
            cx = sum(p[0] for p in pts_canvas) / len(pts_canvas)
            cy = sum(p[1] for p in pts_canvas) / len(pts_canvas)
            if self.roi_stats:
                s = self.roi_stats
                text = f"N={s['N']}  μ={s['mean']:.2f}  σ={s['std']:.2f}\nmed={s['median']:.2f}  IQR={s['iqr']:.2f}"
                tid = self.canvas.create_text(
                    cx,
                    cy,
                    text=text,
                    fill="black",
                    anchor="n",
                    font=("TkDefaultFont", 9),
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
