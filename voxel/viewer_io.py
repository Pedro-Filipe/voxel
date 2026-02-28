"""Folder I/O mixin for DICOMViewer.

Handles recursive folder scanning, metadata reading, study/series/instance
hierarchy construction, and progress reporting during loading.
"""

import os
import threading

from tkinter import messagebox

import pydicom

from .lru_cache import LRUCache
from .utils_dicom import is_dicom_file


class IOLoadMixin:
    """Mixin that provides folder-loading and DICOM-scanning capabilities."""

    def open_folder(self):
        """Show a folder selection dialog and load the chosen folder."""
        from tkinter import filedialog

        folder = filedialog.askdirectory(title="Select folder with DICOM files")
        if not folder:
            return
        self.load_folder(folder)

    def load_folder(self, folder):
        """Start loading a folder of DICOM files in a background thread.

        Resets internal caches, shows progress in the bottom status bar,
        and launches a worker thread to scan for DICOM files and build
        the study/series/instance hierarchy.

        Args:
            folder (str): Path to the folder to scan recursively for
                DICOM files.
        """
        self.folder = folder
        self.metadata_cache = {}
        self.pixel_cache = LRUCache(max_items=8)

        # Show progress in the bottom status bar
        self.status_bar_label.config(text=f"{folder} — scanning for DICOM files...")
        self.status_bar_progress.config(mode="indeterminate", maximum=100)
        self.status_bar_progress.pack(side="right", padx=(6, 6), pady=2)
        self.status_bar_progress.start(15)
        self.update_idletasks()

        # Disable Open button while working
        self.btn_open.config(state="disabled")

        def worker():
            # This runs in a background thread — NO direct Tk calls here!
            try:
                files = self._scan_dicom_files(folder)
                total = len(files)

                # Switch to determinate progress now that we know the total
                def _start_determinate(total=total):
                    self.status_bar_progress.stop()
                    self.status_bar_progress.config(
                        mode="determinate", maximum=max(total, 1), value=0
                    )
                    self.status_bar_label.config(
                        text=f"Loading DICOM metadata: 0 / {total}"
                    )

                self.after(0, _start_determinate)

                def progress_cb(i, total=total):
                    self.after(
                        0,
                        lambda i=i: (
                            self.status_bar_progress.config(value=i),
                            self.status_bar_label.config(
                                text=f"Loading DICOM metadata: {i} / {total}"
                            ),
                        ),
                    )

                series_hierarchy, metadata_cache = self._build_series_hierarchy_thread(
                    files, progress_cb
                )
            except Exception as exc:
                self.after(0, lambda exc=exc: messagebox.showerror("Error", str(exc)))
                files = []
                series_hierarchy = {}
                metadata_cache = {}

            # Hand results back to the main thread
            self.after(
                0,
                lambda: self._finish_load_folder(
                    folder, files, series_hierarchy, metadata_cache
                ),
            )

        threading.Thread(target=worker, daemon=True).start()

    def _scan_dicom_files(self, folder):
        """Recursively scan *folder* and return a sorted list of DICOM paths.

        Uses file extensions and a lightweight signature check via
        :func:`.utils_dicom.is_dicom_file` to identify DICOM files.

        Args:
            folder (str): Root folder to scan.

        Returns:
            list[str]: Sorted list of absolute paths to DICOM files.
        """
        out = []
        candidates = []
        for root, _, files in os.walk(folder):
            for name in files:
                candidates.append(os.path.join(root, name))

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

    def _build_series_hierarchy_thread(self, files, progress_cb=None):
        """Build the study/series/instance hierarchy in a worker thread.

        Reads minimal DICOM metadata (without pixel data) for each file
        and organises files into a nested dictionary:

        - study UID → series UID → list of instances.

        Safe to call from a background thread (no Tk calls).

        Args:
            files (list[str]): List of absolute file paths to process.
            progress_cb (callable | None): Optional ``progress_cb(i)``
                called after each file is processed (1-based count).

        Returns:
            tuple[dict, dict]: ``(series_hierarchy, metadata_cache)``.
        """
        series_hierarchy = {}
        metadata_cache = {}

        for idx, path in enumerate(files, start=1):
            try:
                ds = pydicom.dcmread(path, stop_before_pixels=True, force=True)
                metadata_cache[path] = ds
            except Exception:
                if progress_cb is not None:
                    progress_cb(idx)
                continue

            if progress_cb is not None:
                progress_cb(idx)

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

        # Sort instances by InstanceNumber
        for study_node in series_hierarchy.values():
            for series_node in study_node["series"].values():
                series_node["instances"].sort(
                    key=lambda it: (
                        float(it["instance_number"])
                        if it["instance_number"] not in (None, "")
                        else 1e12
                    )
                )

        return series_hierarchy, metadata_cache

    def _finish_load_folder(self, folder, files, series_hierarchy, metadata_cache):
        """Finalise folder loading on the main thread.

        Updates internal state, repopulates the file tree, resets the
        header tree and image canvas, and shows a status message.

        Args:
            folder (str): Path of the folder that was scanned.
            files (list[str]): List of discovered DICOM file paths.
            series_hierarchy (dict): Built hierarchy for the files.
            metadata_cache (dict[str, pydicom.Dataset]): Metadata cache.
        """
        self.btn_open.config(state="normal")

        # Stop / hide progress bar and update status bar
        self.status_bar_progress.stop()
        self.status_bar_progress.pack_forget()
        count = len(files)
        self.status_bar_label.config(text=f"{folder} — {count} DICOM file(s) loaded")

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

        self.update_idletasks()

        self.current_index = -1
        self.current_ds = None
        self.canvas.delete("all")
        for item in self.hdr_tree.get_children():
            self.hdr_tree.delete(item)
        self._update_frame_controls(1, 1)

        self._populate_file_tree()

        if not self.filtered_files:
            messagebox.showinfo(
                "No DICOM files", "No DICOM files found in the selected folder."
            )

    def _build_series_hierarchy(self):
        """Build the series hierarchy from ``self.files`` on the main thread.

        Uses the existing ``metadata_cache`` when available, otherwise reads
        minimal metadata for each file.  Populates ``self.series_hierarchy``
        in place.
        """
        self.series_hierarchy = {}

        for path in self.files:
            try:
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

            study_node = self.series_hierarchy.setdefault(
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

        for study_node in self.series_hierarchy.values():
            for series_node in study_node["series"].values():
                series_node["instances"].sort(
                    key=lambda it: (
                        float(it["instance_number"])
                        if it["instance_number"] not in (None, "")
                        else 1e12
                    )
                )
