from __future__ import annotations

"""
Export your Anki review history (revlog) as JSONL so you can analyze it with an LLM (ChatGPT, Claude, local models, etc.) **without** using any API.
- One JSON object per review (not per card)  
- Optional filters: time range, tags, minimum interval 
- Flexible schema: verbose or compact (short keys, smaller size)  
- Designed for decks you want to deeply analyze (e.g. language learning, kanji, med school, etc.)
"""

import json
import os
import time
from dataclasses import dataclass
from typing import List, Optional
import re
import html

from aqt import mw
from aqt.qt import (
    QAction,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QMessageBox,
    QPushButton,
    QDesktopServices,
    QUrl,
    QGuiApplication,
)
from aqt.utils import qconnect, tooltip
from aqt.operations import QueryOp
from anki.utils import ids2str
from anki.collection import Collection

# --------------------------------------------------------------------
# Data classes
# --------------------------------------------------------------------


@dataclass
class TimeRangeOption:
    """Predefined time range option for the export dialog."""

    label: str
    days: Optional[int]  # None == no time limit


@dataclass
class ExportResult:
    """Summary information returned by the export function."""

    count: int
    first_ts_ms: Optional[int]
    last_ts_ms: Optional[int]
    deck_name: str


# --------------------------------------------------------------------
# Time range options
# --------------------------------------------------------------------


TIME_RANGE_OPTIONS: List[TimeRangeOption] = [
    TimeRangeOption("Last day (24h)", 1),
    TimeRangeOption("Last week (7 days)", 7),
    TimeRangeOption("Last month (30 days)", 30),
    TimeRangeOption("Last 3 months (90 days)", 90),
    TimeRangeOption("Last year (365 days)", 365),
    TimeRangeOption("All history", None),
]

# --------------------------------------------------------------------
# Main dialog
# --------------------------------------------------------------------


class LLMStatsDialog(QDialog):
    """Configuration dialog for the LLM stats export."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export LLM Stats (revlog)")
        self.resize(480, 260)

        # Deck selection
        self.deck_combo = QComboBox(self)
        self._populate_decks()

        # Predefined time range selection
        self.range_combo = QComboBox(self)
        for opt in TIME_RANGE_OPTIONS:
            self.range_combo.addItem(opt.label, opt.days)

        # Custom number of days (optional)
        self.custom_days_spin = QSpinBox(self)
        # 0 = disabled → use the predefined time range instead
        self.custom_days_spin.setRange(0, 3650)
        self.custom_days_spin.setValue(0)
        self.custom_days_spin.setToolTip(
            "Advanced: custom number of days. "
            "Leave 0 to use the selected predefined range above."
        )
        custom_lbl = QLabel("Custom days (optional)")

        # Output path
        self.path_edit = QLineEdit(self)
        self.path_edit.setPlaceholderText("Output file path (.jsonl)")
        self.browse_button = QLabel('<a href="#">Browse…</a>', self)
        self.browse_button.setOpenExternalLinks(False)
        self.browse_button.linkActivated.connect(self._browse)

        # Field indices to export (optional)
        self.field_indexes_edit = QLineEdit(self)
        self.field_indexes_edit.setPlaceholderText("e.g. 0,1,2 (leave empty = all)")
        self.field_indexes_edit.setToolTip(
            "Indexes of note fields to export (0 = first field). "
            "Leave empty to export all fields."
        )

        # Filters: tags
        self.tags_edit = QLineEdit(self)
        self.tags_edit.setPlaceholderText("e.g. tag1,tag2 (leave empty = no filter)")
        self.tags_edit.setToolTip(
            "Comma-separated tag names. "
            "Only reviews whose note has at least one of these tags will be exported."
        )

        # Filters: minimum interval (days)
        self.min_interval_spin = QSpinBox(self)
        self.min_interval_spin.setRange(0, 365000)
        self.min_interval_spin.setValue(0)
        self.min_interval_spin.setToolTip(
            "Minimum interval (in days) for the review's new interval (r.ivl). "
            "Leave 0 to disable this filter."
        )

        # Export schema options
        self.include_ids_checkbox = QCheckBox("Include card/note/deck IDs")
        self.include_ids_checkbox.setChecked(False)
        self.include_ids_checkbox.setToolTip(
            "If checked, include card_id, note_id and deck_id in the JSON output."
        )

        self.include_deck_name_checkbox = QCheckBox("Include deck name")
        self.include_deck_name_checkbox.setChecked(False)
        self.include_deck_name_checkbox.setToolTip(
            "If checked, include deck_name for each review."
        )

        self.include_ts_ms_checkbox = QCheckBox("Include raw timestamp (ts_ms)")
        self.include_ts_ms_checkbox.setChecked(False)
        self.include_ts_ms_checkbox.setToolTip(
            "If checked, include the raw millisecond timestamp (ts_ms) in addition to ts_iso."
        )

        # Compact schema option
        self.compact_schema_checkbox = QCheckBox(
            "Compact schema (short keys, no IDs/deck name)"
        )
        self.compact_schema_checkbox.setChecked(False)
        self.compact_schema_checkbox.setToolTip(
            "If checked, use a smaller JSON schema:\n"
            '  {"t": "...", "rt": ..., "e": ..., "i": ..., "li": ..., '
            '"f": ..., "ms": ..., "flds": [...]}'
        )

        # Form layout
        form = QFormLayout()
        form.addRow("Deck:", self.deck_combo)
        form.addRow("Time range:", self.range_combo)

        custom_layout = QHBoxLayout()
        custom_layout.addWidget(custom_lbl)
        custom_layout.addWidget(self.custom_days_spin)
        form.addRow(custom_layout)

        form.addRow("Fields to export:", self.field_indexes_edit)
        form.addRow("Filter by tags:", self.tags_edit)
        form.addRow("Minimum interval (days):", self.min_interval_spin)

        # Export schema options row
        schema_layout = QVBoxLayout()
        schema_layout.addWidget(self.include_ids_checkbox)
        schema_layout.addWidget(self.include_deck_name_checkbox)
        schema_layout.addWidget(self.include_ts_ms_checkbox)
        schema_layout.addWidget(self.compact_schema_checkbox)
        form.addRow("Export schema:", schema_layout)

        path_layout = QHBoxLayout()
        path_layout.addWidget(self.path_edit)
        path_layout.addWidget(self.browse_button)
        form.addRow("File:", path_layout)

        # OK / Cancel buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.setLayout(layout)

        # Default output path in the user's profile folder
        default_path = os.path.join(
            mw.pm.profileFolder(),
            "llm_review_stats.jsonl",
        )
        self.path_edit.setText(default_path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _populate_decks(self) -> None:
        """Populate the deck list using mw.col.decks.all_names_and_ids()."""
        self.deck_combo.clear()
        decks = list(
            mw.col.decks.all_names_and_ids()
        )  # returns (name, id) on recent Anki versions
        decks.sort(key=lambda d: d.name.lower())
        for d in decks:
            self.deck_combo.addItem(d.name, d.id)

    def _browse(self) -> None:
        """Open a file dialog to choose the export path."""
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Choose output file",
            self.path_edit.text(),
            "JSON Lines (*.jsonl);;All files (*.*)",
        )
        if path:
            self.path_edit.setText(path)

    # ------------------------------------------------------------------
    # Public helpers to read dialog state
    # ------------------------------------------------------------------

    def selected_deck_id(self) -> int:
        """Return the selected deck id."""
        return int(self.deck_combo.currentData())

    def selected_days(self) -> Optional[int]:
        """
        Return the number of days to export.

        If a custom number of days is specified (> 0), it takes precedence.
        Otherwise, the value from the predefined range is used (may be None).
        """
        custom_days = self.custom_days_spin.value()
        if custom_days and custom_days > 0:
            return custom_days
        return self.range_combo.currentData()

    def selected_field_indexes(self) -> Optional[list[int]]:
        """
        Return a list of 0-based field indices to export, or None if the user
        left the field empty (meaning: export all fields).
        """
        text = self.field_indexes_edit.text().strip()
        if not text:
            return None

        idxs: list[int] = []
        for part in text.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                i = int(part)
                if i >= 0:
                    idxs.append(i)
            except ValueError:
                # Silently ignore invalid values
                continue

        return idxs or None

    def selected_tags(self) -> Optional[list[str]]:
        """
        Return a list of tag names for filtering, or None if no tags were given.

        Only reviews whose note has at least one of these tags will be exported.
        """
        text = self.tags_edit.text().strip()
        if not text:
            return None

        # Accept "tag1,tag2" or "tag1, tag2"
        tags: list[str] = []
        for part in text.replace(",", " ").split():
            t = part.strip().lower()
            if t:
                tags.append(t)

        return tags or None

    def min_interval(self) -> Optional[int]:
        """
        Return the minimum interval (in days) for the review's new interval (r.ivl),
        or None if no minimum is set.
        """
        value = self.min_interval_spin.value()
        if value > 0:
            return value
        return None

    def include_ids(self) -> bool:
        """Whether to include card/note/deck IDs in the export."""
        return self.include_ids_checkbox.isChecked()

    def include_deck_name(self) -> bool:
        """Whether to include deck_name in the export."""
        return self.include_deck_name_checkbox.isChecked()

    def include_ts_ms(self) -> bool:
        """Whether to include the raw timestamp (ts_ms) in the export."""
        return self.include_ts_ms_checkbox.isChecked()

    def compact_schema(self) -> bool:
        """Whether to use the compact JSON schema."""
        return self.compact_schema_checkbox.isChecked()

    def output_path(self) -> str:
        """Return the output file path."""
        return self.path_edit.text().strip()


# --------------------------------------------------------------------
# Deck utilities
# --------------------------------------------------------------------


def _deck_and_child_ids(col: Collection, deck_id: int) -> List[int]:
    """
    Return the deck id and all child deck ids, in a way that works across
    multiple Anki versions.
    """
    decks: List[int] = [deck_id]

    # Newer Anki: DeckManager.deck_and_child_ids()
    try:
        manager = col.decks
        if hasattr(manager, "deck_and_child_ids"):
            return list(manager.deck_and_child_ids(deck_id))
    except Exception:
        # Fallback to older behavior below
        pass

    # Fallback: recursively descend via decks.children()
    def collect(did: int, acc: List[int]) -> None:
        for name, child_id in col.decks.children(did):
            acc.append(child_id)
            collect(child_id, acc)

    collect(deck_id, decks)
    return decks


# --------------------------------------------------------------------
# Field cleaning utilities
# --------------------------------------------------------------------

_SOUND_RE = re.compile(r"\[sound:[^\]]+\]", re.IGNORECASE)
_IMG_TAG_RE = re.compile(r"<img[^>]*>", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_field_value(text: str) -> str:
    """
    Clean an Anki field value:

    - remove [sound:xxx.mp3] references
    - remove <img ...> tags
    - unescape HTML entities (&nbsp; → space, etc.)
    - remove remaining HTML tags
    - normalize whitespace
    """
    if not text:
        return ""

    # Remove [sound:...]
    text = _SOUND_RE.sub("", text)

    # Remove <img ...>
    text = _IMG_TAG_RE.sub("", text)

    # Unescape HTML entities (&nbsp; → space, etc.)
    text = html.unescape(text)

    # Remove remaining HTML tags (<b>, <span>, etc.)
    text = _HTML_TAG_RE.sub("", text)

    # Collapse whitespace
    text = _WS_RE.sub(" ", text).strip()

    return text


# --------------------------------------------------------------------
# Export logic (revlog → JSONL)
# --------------------------------------------------------------------


def export_llm_stats(
    col: Collection,
    deck_id: int,
    days: Optional[int],
    out_path: str,
    field_indexes: Optional[list[int]] = None,
    tags_filter: Optional[list[str]] = None,
    min_interval: Optional[int] = None,
    include_ids: bool = False,
    include_deck_name: bool = False,
    include_ts_ms: bool = False,
    compact_schema: bool = False,
) -> ExportResult:
    """
    Heavy function executed in a background thread via QueryOp.

    Returns an ExportResult with summary information.
    """
    dids = _deck_and_child_ids(col, deck_id)
    dids_str = ids2str(dids)

    where_clauses = [f"c.did IN {dids_str}"]
    params: list = []

    # Time filter on revlog.id (timestamp in ms since epoch)
    if days is not None:
        now_sec = time.time()
        cutoff_ms = int((now_sec - days * 86400) * 1000)
        where_clauses.append("r.id >= ?")
        params.append(cutoff_ms)

    # Tag filter (note tags)
    # Anki stores tags in n.tags as " tag1 tag2 ", all lowercased.
    if tags_filter:
        tag_clauses: list[str] = []
        for tag in tags_filter:
            tag_clauses.append("n.tags LIKE ?")
            params.append(f"% {tag} %")
        if tag_clauses:
            where_clauses.append("(" + " OR ".join(tag_clauses) + ")")

    # Minimum interval filter (on the new interval r.ivl)
    if min_interval is not None:
        where_clauses.append("r.ivl >= ?")
        params.append(min_interval)

    where_sql = " AND ".join(where_clauses)

    # Join revlog + cards + notes to get deck and fields in one query,
    # which minimizes Python/SQLite round-trips (important for large revlogs).
    # n.flds is a string with fields separated by \x1f.
    sql = f"""
SELECT
    r.id,           -- timestamp (ms)
    r.cid,          -- card id
    c.nid,          -- note id
    c.did,          -- deck id
    r.ease,         -- chosen button (1-4)
    r.ivl,          -- new interval (days)
    r.lastIvl,      -- previous interval (days)
    r.factor,       -- ease factor
    r.time,         -- response time (ms)
    r.type,         -- review type (0=learn,1=review,2=relearn,3=filtered)
    n.flds,         -- concatenated note fields
    n.tags          -- note tags (for info, even if we don't export them yet)
FROM revlog r
JOIN cards c ON c.id = r.cid
JOIN notes n ON n.id = c.nid
WHERE {where_sql}
ORDER BY r.id
"""

    # Preload deck names to avoid lookups in the main loop
    deck_names = {d.id: d.name for d in col.decks.all_names_and_ids()}
    root_deck_name = deck_names.get(deck_id, "<unknown>")

    # Stream writing to JSONL file (one review per line)
    folder = os.path.dirname(out_path)
    if folder:
        os.makedirs(folder, exist_ok=True)

    count = 0
    first_ts_ms: Optional[int] = None
    last_ts_ms: Optional[int] = None

    with open(out_path, "w", encoding="utf-8") as fh:
        for row in col.db.execute(sql, *params):
            (
                ts_ms,
                cid,
                nid,
                did,
                ease,
                ivl,
                last_ivl,
                factor,
                review_time_ms,
                rev_type,
                flds,
                _note_tags,  # currently unused, but fetched for completeness
            ) = row

            # Track actual date range covered (based on exported reviews)
            if first_ts_ms is None:
                first_ts_ms = ts_ms
            last_ts_ms = ts_ms

            raw_fields = flds.split("\x1f") if flds else []

            # Select which fields to export (by index)
            if field_indexes is not None:
                selected: list[str] = []
                for idx in field_indexes:
                    if 0 <= idx < len(raw_fields):
                        selected.append(clean_field_value(raw_fields[idx]))
                fields = selected
            else:
                # All fields, cleaned
                fields = [clean_field_value(v) for v in raw_fields]

            # Build JSON object (two modes: full vs compact)
            ts_iso = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts_ms / 1000.0)
            )

            if compact_schema:
                # Compact schema: short keys, no IDs/deck_name/ts_ms
                # You should explain this schema in your LLM prompt.
                obj: dict = {
                    "t": ts_iso,          # timestamp ISO
                    "rt": rev_type,       # review type (0,1,2,3)
                    "e": ease,            # ease (1–4)
                    "i": ivl,             # new interval (days)
                    "li": last_ivl,       # previous interval (days)
                    "f": factor,          # ease factor
                    "ms": review_time_ms, # answer time in ms
                    "flds": fields,       # list of cleaned fields
                }
            else:
                # Verbose schema (original)
                obj: dict = {
                    "ts_iso": ts_iso,
                    "ease": ease,
                    "interval": ivl,
                    "last_interval": last_ivl,
                    "factor": factor,
                    "review_time_ms": review_time_ms,
                    "review_type": rev_type,
                    "fields": fields,
                }

                # Optional schema elements
                if include_ts_ms:
                    obj["ts_ms"] = ts_ms

                if include_ids:
                    obj["card_id"] = cid
                    obj["note_id"] = nid
                    obj["deck_id"] = did

                if include_deck_name:
                    obj["deck_name"] = deck_names.get(did, "<unknown>")

            fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
            count += 1

    return ExportResult(
        count=count,
        first_ts_ms=first_ts_ms,
        last_ts_ms=last_ts_ms,
        deck_name=root_deck_name,
    )

# --------------------------------------------------------------------
# UI glue + background operation
# --------------------------------------------------------------------


def _format_date_range(first_ts_ms: Optional[int], last_ts_ms: Optional[int]) -> str:
    """Format the date range for display in the summary."""
    if first_ts_ms is None or last_ts_ms is None:
        return "N/A"

    # Use local time for display
    start_str = time.strftime(
        "%Y-%m-%d", time.localtime(first_ts_ms / 1000.0)
    )
    end_str = time.strftime(
        "%Y-%m-%d", time.localtime(last_ts_ms / 1000.0)
    )
    if start_str == end_str:
        return start_str
    return f"{start_str} – {end_str}"


def on_export_llm_stats() -> None:
    """Show the configuration dialog and run the export in the background."""
    if mw.col is None:
        return

    dlg = LLMStatsDialog(mw)
    if not dlg.exec():
        return

    deck_id = dlg.selected_deck_id()
    days = dlg.selected_days()
    out_path = dlg.output_path()
    field_indexes = dlg.selected_field_indexes()
    tags_filter = dlg.selected_tags()
    min_interval = dlg.min_interval()
    include_ids = dlg.include_ids()
    include_deck_name = dlg.include_deck_name()
    include_ts_ms = dlg.include_ts_ms()
    compact_schema = dlg.compact_schema()

    if not out_path:
        tooltip("Invalid file path.")
        return

    def _on_success(result: ExportResult) -> None:
        # Always show a summary dialog, even if 0 reviews were exported.
        date_range = _format_date_range(result.first_ts_ms, result.last_ts_ms)

        extra_info = ""
        if result.count == 0:
            extra_info = (
                "\n\nNo reviews matched the selected filters.\n"
                "Try:\n"
                "- Time range: All history\n"
                "- Clear tag filter\n"
                "- Minimum interval: 0\n"
            )

        text_lines = [
            "Export complete.",
            "",
            f"Deck: {result.deck_name}",
            f"Reviews exported: {result.count}",
            f"Date range: {date_range}",
            "",
            f"File: {out_path}",
        ]
        if extra_info:
            text_lines.append(extra_info)

        summary = "\n".join(text_lines)

        box = QMessageBox(mw)
        box.setWindowTitle("LLM Stats Export")
        box.setText(summary)

        # Buttons: Open folder, Copy path, OK
        open_btn = QPushButton("Open folder")
        copy_btn = QPushButton("Copy path")
        ok_btn = QPushButton("OK")

        # PyQt6 style roles
        box.addButton(open_btn, QMessageBox.ButtonRole.ActionRole)
        box.addButton(copy_btn, QMessageBox.ButtonRole.ActionRole)
        box.addButton(ok_btn, QMessageBox.ButtonRole.AcceptRole)

        box.exec()

        clicked = box.clickedButton()
        if clicked is open_btn:
            folder = os.path.dirname(out_path)
            if folder:
                QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
        elif clicked is copy_btn:
            QGuiApplication.clipboard().setText(out_path)

    def _on_failure(exc: Exception) -> None:
        box = QMessageBox(mw)
        box.setWindowTitle("LLM Stats Export - Error")
        box.setText(f"Error during export:\n{exc}")
        box.setIcon(QMessageBox.Critical)
        box.exec()

    op = QueryOp(
        parent=mw,
        op=lambda col: export_llm_stats(
            col=col,
            deck_id=deck_id,
            days=days,
            out_path=out_path,
            field_indexes=field_indexes,
            tags_filter=tags_filter,
            min_interval=min_interval,
            include_ids=include_ids,
            include_deck_name=include_deck_name,
            include_ts_ms=include_ts_ms,
            compact_schema=compact_schema,
        ),
        success=_on_success,
    )
    op.failure(_on_failure)
    op.with_progress(label="Exporting review stats…").run_in_background()


# --------------------------------------------------------------------
# Add menu entry under Tools
# --------------------------------------------------------------------

action = QAction("Export LLM Stats…", mw)
qconnect(action.triggered, on_export_llm_stats)
mw.form.menuTools.addAction(action)

