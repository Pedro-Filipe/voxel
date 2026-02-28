"""File-tree and header-tree mixin for DICOMViewer.

Handles the study/series/instance Treeview on the left panel and the
DICOM-header Treeview on the right panel, including filtering logic.
"""

import os

import tkinter as tk

from .utils_dicom import format_tag, safe_str


class TreeMixin:
    """Mixin providing file-tree and header-tree population and filtering."""

    # ------------------------------------------------------------------
    # File tree – selection
    # ------------------------------------------------------------------

    def _on_tree_select(self, event):
        """Handle selection changes in the file Treeview.

        When a leaf (instance) node is selected, updates
        ``current_index`` and loads the corresponding file.

        Args:
            event: Tkinter event object (unused except for signature).
        """
        sel = self.file_tree.selection()
        if not sel:
            return
        item_id = sel[0]
        path = self.tree_item_to_path.get(item_id)
        if not path:
            return  # clicked a study or series node
        if path in self.filtered_files:
            self.current_index = self.filtered_files.index(path)
        self.load_file(path)

    # ------------------------------------------------------------------
    # File tree – filter
    # ------------------------------------------------------------------

    def _clear_file_filter(self):
        """Clear the file tree filter and rebuild the file tree."""
        self.file_filter_var.set("")
        self._apply_file_filter()

    def _on_file_filter_change(self, event=None):
        """Callback for changes in the file filter entry.

        Args:
            event: Tkinter event object, or ``None`` when called
                programmatically.
        """
        self._apply_file_filter()

    def _apply_file_filter(self):
        """Rebuild the file tree using the current filter pattern."""
        if not self.folder:
            return
        self._populate_file_tree()

    # ------------------------------------------------------------------
    # File tree – expand/collapse
    # ------------------------------------------------------------------

    def _on_file_expand_all_toggle(self):
        """Toggle expand/collapse for all nodes in the file tree."""
        self._set_file_tree_open_all(self.file_expand_all.get())

    def _set_file_tree_open_all(self, open_flag):
        """Recursively set open/closed state for all file-tree nodes.

        Args:
            open_flag (bool): ``True`` to expand, ``False`` to collapse.
        """

        def set_node(n):
            try:
                self.file_tree.item(n, open=open_flag)
            except Exception:
                pass
            for child in self.file_tree.get_children(n):
                set_node(child)

        for root in self.file_tree.get_children(""):
            set_node(root)

    # ------------------------------------------------------------------
    # File tree – populate
    # ------------------------------------------------------------------

    def _populate_file_tree(self):
        """Rebuild the study/series/instance Treeview.

        Filters instances by the pattern in ``file_filter_var`` and
        tries to restore the previously selected file.
        """
        # Remember current selection
        current_path = None
        if 0 <= self.current_index < len(self.filtered_files):
            current_path = self.filtered_files[self.current_index]

        for item in self.file_tree.get_children():
            self.file_tree.delete(item)
        self.tree_item_to_path.clear()

        if not self.series_hierarchy:
            return

        pattern = self.file_filter_var.get().strip().lower()

        def match_text(s):
            return pattern == "" or pattern in str(s).lower()

        self.filtered_files = []
        open_flag = self.file_expand_all.get()

        for study_uid, study_node in sorted(
            self.series_hierarchy.items(), key=lambda kv: kv[0]
        ):
            study_desc = study_node["study_desc"] or "Study"
            patient_name = study_node["patient_name"] or ""
            study_date = study_node["study_date"] or ""
            study_label = study_desc
            info = f"Patient: {patient_name}  Date: {study_date}"

            study_id = self.file_tree.insert(
                "", tk.END, text=study_label, values=(info,), open=open_flag
            )

            added_any_series = False

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

                series_id = self.file_tree.insert(
                    study_id,
                    tk.END,
                    text=series_label,
                    values=(series_info,),
                    open=open_flag,
                )

                added_any_instance = False

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

                if not added_any_instance:
                    self.file_tree.delete(series_id)

            if not added_any_series:
                self.file_tree.delete(study_id)

        self._set_file_tree_open_all(self.file_expand_all.get())

        # Restore selection
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
        """Select the Treeview leaf for *path*, expanding ancestors.

        Args:
            path (str): Absolute path to the DICOM file to select.
        """
        for item_id, p in self.tree_item_to_path.items():
            if p == path:
                parent = self.file_tree.parent(item_id)
                while parent:
                    self.file_tree.item(parent, open=True)
                    parent = self.file_tree.parent(parent)
                self.file_tree.selection_set(item_id)
                self.file_tree.see(item_id)
                self.load_file(path)
                break

    # ------------------------------------------------------------------
    # Header tree – filter
    # ------------------------------------------------------------------

    def _clear_header_filter(self):
        """Clear the header filter and rebuild the header tree."""
        self.header_filter_var.set("")
        self._rebuild_header_tree()

    def _on_header_filter_change(self, event=None):
        """Debounced callback for header filter changes (150 ms).

        Args:
            event: Tkinter event object, or ``None``.
        """
        if self._header_filter_after_id is not None:
            self.after_cancel(self._header_filter_after_id)
        self._header_filter_after_id = self.after(150, self._rebuild_header_tree)

    def _on_header_scope_change(self, event=None):
        """Handle changes in the header scope combobox."""
        self._rebuild_header_tree()

    # ------------------------------------------------------------------
    # Header tree – expand/collapse
    # ------------------------------------------------------------------

    def _on_expand_all_toggle(self):
        """Toggle expand/collapse for all header tree nodes."""
        self._set_tree_open_all(self.header_expand_all.get())

    def _set_tree_open_all(self, open_flag):
        """Recursively set open/closed state for all header-tree nodes.

        Args:
            open_flag (bool): ``True`` to expand, ``False`` to collapse.
        """

        def set_node(n):
            try:
                self.hdr_tree.item(n, open=open_flag)
            except Exception:
                pass
            for child in self.hdr_tree.get_children(n):
                set_node(child)

        for root in self.hdr_tree.get_children(""):
            set_node(root)

    # ------------------------------------------------------------------
    # Header tree – rebuild
    # ------------------------------------------------------------------

    def _rebuild_header_tree(self):
        """Rebuild the DICOM header Treeview according to scope and filter.

        Clears the existing header tree and repopulates it based on the
        selected scope, the current filter text, and the current frame
        index for frame-based scopes.
        """
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
            # Frame-scoped variants
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

        self._set_tree_open_all(self.header_expand_all.get())

    def _insert_summary(self, root, pattern, open_flag):
        """Insert a synthetic Summary node with commonly inspected tags.

        Args:
            root: Treeview item ID of the parent node.
            pattern (str): Lowercased filter pattern.
            open_flag (bool): Whether to initially expand the node.
        """
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
            val_s = safe_str(val)
            if self._matches(pattern, [tag_str, name, val_s, "Summary"]):
                self.hdr_tree.insert(
                    sum_root,
                    tk.END,
                    text=name,
                    values=(tag_str, name, val_s),
                    open=open_flag,
                )
                matched_any = True
        if not matched_any:
            self.hdr_tree.delete(sum_root)

    def _insert_dataset_recursive(self, parent_id, dataset, pattern, open_flag):
        """Recursively insert a DICOM dataset into the header tree.

        Prunes nodes that do not match the filter pattern.

        Args:
            parent_id: Treeview item ID of the parent node.
            dataset (pydicom.Dataset): Dataset or sequence item.
            pattern (str): Lowercased search pattern.
            open_flag (bool): Whether newly created nodes are expanded.

        Returns:
            bool: ``True`` if at least one node was inserted.
        """
        inserted_any = False
        for elem in dataset:
            if elem.VR == "SQ":
                seq_name = elem.keyword or elem.name or "Sequence"
                tag_str = format_tag(elem.tag)
                seq_matches_self = self._matches(
                    pattern,
                    [tag_str, seq_name, f"Sequence ({len(elem.value)} item(s))"],
                )
                seq_id = self.hdr_tree.insert(
                    parent_id,
                    tk.END,
                    text=seq_name,
                    values=(
                        tag_str,
                        seq_name,
                        f"Sequence ({len(elem.value)} item(s))",
                    ),
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
                if self._matches(pattern, [tag_str, name, val_preview]):
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
        """Return whether any field contains *pattern* (case-insensitive).

        Args:
            pattern (str): Lowercased filter pattern. Empty → always True.
            fields (Iterable[Any]): Values to search.

        Returns:
            bool: ``True`` if match found or pattern is empty.
        """
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
