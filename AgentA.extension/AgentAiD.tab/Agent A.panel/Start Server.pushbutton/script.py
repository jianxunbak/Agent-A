#! python3
# -*- coding: utf-8 -*-
"""
Gemini MCP Server - Start Button Script

Uses direct drain_queue() on the script thread + Application.DoEvents()
to pump Windows messages, preventing the STA thread deadlock that occurs
when Revit's view regeneration fires COM callbacks after Transaction.Commit().
"""
import sys
import os
import time

# Robust path discovery
try:
    _cur_dir = os.path.dirname(os.path.abspath(__file__))
    # script.py is in .extension/.tab/.panel/.pushbutton/script.py
    # We need .extension/ level
    ext_root = os.path.dirname(os.path.dirname(os.path.dirname(_cur_dir)))
    lib_path = os.path.join(ext_root, 'lib')

    if ext_root not in sys.path:
        sys.path.append(ext_root)
    if lib_path not in sys.path:
        sys.path.append(lib_path)
except Exception as e:
    print("Path initialization failed: " + str(e))

# Hard reset of revit_mcp modules to force reload from disk
for m in list(sys.modules.keys()):
    if 'revit_mcp' in m:
        del sys.modules[m]

from pyrevit import revit, DB, UI, HOST_APP, forms, script
from Autodesk.Revit.UI import TaskDialog

# Track initialization status
_init_success = False
try:
    from revit_mcp.gemini_client import client
    from revit_mcp.dispatcher import orchestrator
    from revit_mcp import bridge
    client.log("UI: Hard module reset completed. v9-STABLE starting.")
    _init_success = True
except Exception as e:
    import traceback
    _init_error = "Pre-load failed: {}\n\nTraceback:\n{}".format(str(e), traceback.format_exc())
    print(_init_error)

    # Dummy client to prevent NameError crashes later
    class DummyClient:
        def log(self, *args, **kwargs): pass
    client = DummyClient()

import clr
import re as _re

# Import message pump - critical for STA thread compatibility
try:
    clr.AddReference('System.Windows.Forms')
    from System.Windows.Forms import Application as WinForms
    _has_doevents = True
except:
    _has_doevents = False

# Set up references for WPF
clr.AddReference("PresentationFramework")
clr.AddReference("PresentationCore")
clr.AddReference("WindowsBase")
clr.AddReference("System.Xaml")
clr.AddReference("System")
import System.Windows.Input
import threading
from System.Windows import Window, HorizontalAlignment, VerticalAlignment, Thickness, CornerRadius, TextWrapping, GridLength, GridUnitType
from System.Windows.Media import Brushes, Color, SolidColorBrush, FontFamily
from System.Windows.Controls import Border, TextBox, TextBlock, RichTextBox, StackPanel, Grid, ColumnDefinition, RowDefinition, ScrollViewer, ScrollBarVisibility
from System.Windows.Documents import (
    Run, Bold, Italic, Underline, Hyperlink,
    FlowDocument, Paragraph, Section, LineBreak, BlockUIContainer,
    Table, TableRowGroup, TableRow, TableCell, TableColumn,
    List as FlowList, ListItem,
)
from System import Uri, UriKind
from System.Diagnostics import Process, ProcessStartInfo
from System.Windows.Interop import WindowInteropHelper
from System.Windows.Threading import DispatcherTimer
from System.Windows.Markup import XamlReader
from System.IO import FileStream, FileMode, FileAccess, FileShare
from System import TimeSpan, Action
from System.Windows import FontWeights, FontStyles


# ── Markdown → WPF renderer ──────────────────────────────────────────────────

def _color(r, g, b):
    return SolidColorBrush(Color.FromRgb(r, g, b))

_COL_H1      = _color(255, 220, 80)
_COL_H2      = _color(180, 220, 255)
_COL_H3      = _color(180, 255, 180)
_COL_CODE    = _color(220, 180, 255)
_COL_WHITE   = Brushes.White
_COL_MUTED   = _color(180, 180, 180)
_COL_TH_BG   = _color(40, 80, 130)
_COL_TR_ALT  = _color(35, 35, 50)
_COL_TR_NORM   = _color(28, 28, 40)
_COL_TR_ACTIVE = _color(18, 55, 22)
_COL_BORDER  = _color(60, 80, 120)
_MONO_FONT   = FontFamily("Consolas, Courier New")

_GREEN_BRUSH = _color(102, 255, 102)
_MARKER_START = u""
_MARKER_END   = u""


_LINK_BRUSH = _color(100, 180, 255)
_URL_RE     = _re.compile(r'(https?://[^\s<>"\')\]]+)')
_MDLINK_RE  = _re.compile(r'\[([^\]]+)\]\((https?://[^\s)]+)\)')


def _on_hyperlink_navigate(sender, e):
    """Open the clicked URL in the user's default browser."""
    try:
        psi = ProcessStartInfo(e.Uri.AbsoluteUri)
        psi.UseShellExecute = True
        Process.Start(psi)
        e.Handled = True
    except Exception:
        pass


def _make_hyperlink(label, url):
    """Build a clickable Hyperlink inline (falls back to plain Run on bad URL)."""
    try:
        h = Hyperlink(Run(label))
        h.NavigateUri = Uri(url, UriKind.Absolute)
        h.Foreground = _LINK_BRUSH
        h.RequestNavigate += _on_hyperlink_navigate
        return h
    except Exception:
        return Run(label)


def _add_text_with_links(tb, text, run_factory=None):
    """Append text to tb.Inlines, turning bare http(s) URLs into Hyperlinks.

    run_factory: callable(str)->Run that produces a styled Run for non-URL
    pieces (so bold/italic styling carries across the split). Defaults to plain
    Run.
    """
    if run_factory is None:
        run_factory = Run
    pos = 0
    for m in _URL_RE.finditer(text):
        if m.start() > pos:
            tb.Inlines.Add(run_factory(text[pos:m.start()]))
        tb.Inlines.Add(_make_hyperlink(m.group(1), m.group(1)))
        pos = m.end()
    if pos < len(text):
        tb.Inlines.Add(run_factory(text[pos:]))


def _apply_inline(tb, text):
    """Parse [label](url), **bold**, *italic*, `code`, bare URLs, and plain
    text into TextBlock Inlines."""
    # Resolve markdown links first by splitting `text` into a sequence of plain
    # string chunks and pre-built Hyperlink inlines, then apply bold/italic/
    # code/URL parsing only to the string chunks.
    pieces = []
    pos = 0
    for m in _MDLINK_RE.finditer(text):
        if m.start() > pos:
            pieces.append(text[pos:m.start()])
        pieces.append(_make_hyperlink(m.group(1), m.group(2)))
        pos = m.end()
    if pos < len(text):
        pieces.append(text[pos:])

    pattern = _re.compile(r'(\*\*(.+?)\*\*|\*(.+?)\*|`([^`]+?)`)')
    for piece in pieces:
        if not isinstance(piece, str):
            tb.Inlines.Add(piece)
            continue
        spos = 0
        for m in pattern.finditer(piece):
            if m.start() > spos:
                _add_text_with_links(tb, piece[spos:m.start()])
            full = m.group(0)
            if full.startswith('**'):
                def _bold(t):
                    r = Run(t); r.FontWeight = FontWeights.Bold; return r
                _add_text_with_links(tb, m.group(2), _bold)
            elif full.startswith('*'):
                def _ital(t):
                    r = Run(t); r.FontStyle = FontStyles.Italic; return r
                _add_text_with_links(tb, m.group(3), _ital)
            elif full.startswith('`'):
                r = Run(m.group(4))
                r.FontFamily = _MONO_FONT
                r.Foreground = _COL_CODE
                tb.Inlines.Add(r)  # don't linkify inside backtick code
            spos = m.end()
        if spos < len(piece):
            _add_text_with_links(tb, piece[spos:])


# ── Enable text selection on TextBlock via the WPF TextEditor trick ──────────
#
# WPF's TextBlock does not natively support selection. The internal
# System.Windows.Documents.TextEditor class can be attached to any
# UIElement that hosts inlines; this is the standard community workaround
# and is used by tools like Visual Studio's output pane. It's "undocumented"
# but stable since .NET 3.5 and only requires reflection to call.
_TEXT_EDITOR_TYPE = None
_REG_CMD_HANDLERS = None
try:
    import System
    _pf_asm = System.Reflection.Assembly.Load("PresentationFramework")
    _TEXT_EDITOR_TYPE = _pf_asm.GetType("System.Windows.Documents.TextEditor")
    if _TEXT_EDITOR_TYPE is not None:
        _REG_CMD_HANDLERS = _TEXT_EDITOR_TYPE.GetMethod(
            "RegisterCommandHandlers",
            System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.NonPublic,
        )
except Exception:
    _TEXT_EDITOR_TYPE = None
    _REG_CMD_HANDLERS = None


_selection_registered_types = set()


def _enable_selection(tb):
    """Make a TextBlock selectable. The TextEditor command handlers are
    registered class-wide on first call; per-instance work after that is just
    Focusable + IBeam cursor so the user gets the right visual cue."""
    if _REG_CMD_HANDLERS is None:
        return
    try:
        t = tb.GetType()
        if t.FullName not in _selection_registered_types:
            # Signature: RegisterCommandHandlers(Type controlType,
            #     bool acceptsRichContent, bool readOnly, bool registerEventListeners)
            _REG_CMD_HANDLERS.Invoke(
                None, System.Array[System.Object]([t, True, True, True])
            )
            _selection_registered_types.add(t.FullName)
        tb.Focusable = True
        tb.Cursor = System.Windows.Input.Cursors.IBeam
    except Exception:
        pass


def _make_tb(text, fg=None, size=None, bold=False, italic=False, wrap=True, font=None):
    tb = TextBlock()
    tb.TextWrapping = TextWrapping.Wrap if wrap else TextWrapping.NoWrap
    tb.Foreground = fg or _COL_WHITE
    if size:
        tb.FontSize = size
    if bold:
        tb.FontWeight = FontWeights.Bold
    if italic:
        tb.FontStyle = FontStyles.Italic
    if font:
        tb.FontFamily = font
    _apply_inline(tb, text)
    _enable_selection(tb)
    return tb


def _table_log(stage, info=""):
    """Bulletproof file-based logger for table-render debugging."""
    try:
        import os as _os
        from revit_mcp.utils import get_appdata_path
        _path = _os.path.join(get_appdata_path("logs"), "table_render_debug.log")
        with open(_path, "a") as _f:
            _f.write("[{0}] {1}\n".format(stage, info))
    except Exception:
        pass


def _is_table_line(line):
    return '|' in line


def _parse_table(lines):
    _table_log("PARSE_ENTER", "lines_in={0}".format(len(lines)))
    rows = []
    for idx, ln in enumerate(lines):
        ln = ln.strip()
        if not ln or _re.match(r'^\|[-| :]+\|$', ln):
            _table_log("PARSE_SKIP", "idx={0} reason={1!r}".format(idx, "separator-or-empty"))
            continue
        cells = [c.strip() for c in ln.strip('|').split('|')]
        _table_log("PARSE_ROW", "idx={0} cells={1} preview={2!r}".format(idx, len(cells), ln[:120]))
        rows.append(cells)
    _table_log("PARSE_EXIT", "rows={0}".format(len(rows)))
    return rows


def _build_table_grid(rows):
    _table_log("BUILD_ENTER", "rows={0}".format(len(rows) if rows else 0))
    if not rows:
        _table_log("BUILD_EXIT", "no rows -> None")
        return None

    try:
        col_count = max(len(r) for r in rows)
        _table_log("BUILD_COLS", "col_count={0} row_widths={1}".format(col_count, [len(r) for r in rows[:5]]))

        # Pick a per-column natural width based on the longest cell content,
        # clamped to a readable range. Columns size to their own content
        # instead of being squashed by the parent width.
        col_widths = []
        for ci in range(col_count):
            longest = 0
            for row in rows:
                if ci < len(row):
                    cell = row[ci].replace(_MARKER_START, "").replace(_MARKER_END, "")
                    if len(cell) > longest:
                        longest = len(cell)
            # ~7px per char + padding, clamped to [70, 220]
            px = max(70, min(220, 14 + longest * 7))
            col_widths.append(px)

        grid = Grid()
        grid.Margin = Thickness(0, 4, 0, 4)
        grid.HorizontalAlignment = HorizontalAlignment.Left

        for w in col_widths:
            cd = ColumnDefinition()
            cd.Width = GridLength(w)
            grid.ColumnDefinitions.Add(cd)
        _table_log("BUILD_COLDEFS_OK", "widths={0}".format(col_widths))

        for ri, row in enumerate(rows):
            rd = RowDefinition()
            rd.Height = GridLength.Auto
            grid.RowDefinitions.Add(rd)

            is_header = (ri == 0)
            is_active = not is_header and any(_MARKER_START in cell for cell in row)
            if is_header:
                bg = _COL_TH_BG
            elif is_active:
                bg = _COL_TR_ACTIVE
            else:
                bg = _COL_TR_ALT if ri % 2 == 0 else _COL_TR_NORM

            for ci in range(col_count):
                cell_text = row[ci] if ci < len(row) else ""
                cell_text = cell_text.replace(_MARKER_START, "").replace(_MARKER_END, "")
                cell_border = Border()
                cell_border.Background = bg
                cell_border.BorderBrush = _COL_BORDER
                cell_border.BorderThickness = Thickness(0.5)
                cell_border.Padding = Thickness(6, 4, 6, 4)

                # wrap=True keeps long cells from forcing the column wider —
                # the cell wraps within its fixed column width instead.
                tb = _make_tb(cell_text, bold=is_header, wrap=True)
                if is_header:
                    tb.Foreground = _color(220, 240, 255)
                elif is_active:
                    tb.Foreground = _GREEN_BRUSH
                cell_border.Child = tb

                Grid.SetRow(cell_border, ri)
                Grid.SetColumn(cell_border, ci)
                grid.Children.Add(cell_border)
            _table_log("BUILD_ROW_OK", "ri={0}".format(ri))

        # Horizontal scroller so wide tables don't get clipped — user can
        # drag the scrollbar to read the right-hand columns.
        scroller = ScrollViewer()
        scroller.HorizontalScrollBarVisibility = ScrollBarVisibility.Auto
        scroller.VerticalScrollBarVisibility = ScrollBarVisibility.Disabled
        scroller.Margin = Thickness(0, 4, 0, 4)
        scroller.Content = grid

        _table_log("BUILD_EXIT", "grid built OK, children={0}".format(grid.Children.Count))
        return scroller
    except Exception as _be:
        import traceback as _tb
        _table_log("BUILD_EXCEPTION", "{0}\n{1}".format(_be, _tb.format_exc()))
        raise


# ── FlowDocument-based renderer (selectable + copyable) ──────────────────────
#
# All AI bubbles render into a FlowDocument hosted inside a read-only
# RichTextBox. That gives the user native Windows text selection across the
# whole bubble (drag to highlight, Ctrl+C, Ctrl+A) which a StackPanel of
# TextBlocks fundamentally cannot do.
#
# Tables and the horizontal-rule separator can't be expressed as flow inlines
# alone, so they're embedded as BlockUIContainer(UIElement) — the
# UIElement-hosted regions are NOT selectable (each cell is its own TextBlock),
# but the prose flowing around them is, which is what users care about for
# copying answers.


class _InlineSink(object):
    """Adapter so the TextBlock-oriented inline helpers can write into any
    InlineCollection (Paragraph.Inlines, Span.Inlines, etc.)."""
    def __init__(self, col):
        self.Inlines = col


def _apply_inline_to(target_inlines, text):
    _apply_inline(_InlineSink(target_inlines), text)


def _add_text_with_links_to(target_inlines, text, run_factory=None):
    _add_text_with_links(_InlineSink(target_inlines), text, run_factory)


def _flow_paragraph(text, fg=None, size=None, bold=False, italic=False,
                    margin=None, font=None):
    p = Paragraph()
    p.Margin = margin if margin is not None else Thickness(0, 1, 0, 1)
    if fg is not None:
        p.Foreground = fg
    else:
        p.Foreground = _COL_WHITE
    if size:
        p.FontSize = size
    if bold:
        p.FontWeight = FontWeights.Bold
    if italic:
        p.FontStyle = FontStyles.Italic
    if font:
        p.FontFamily = font
    _apply_inline_to(p.Inlines, text)
    return p


def _build_flow_table(rows):
    """Build a FlowDocument Table from parsed markdown rows."""
    if not rows:
        return None
    col_count = max(len(r) for r in rows)

    tbl = Table()
    tbl.CellSpacing = 0
    tbl.Margin = Thickness(0, 4, 0, 4)

    for ci in range(col_count):
        longest = 0
        for row in rows:
            if ci < len(row):
                cell = row[ci].replace(_MARKER_START, "").replace(_MARKER_END, "")
                if len(cell) > longest:
                    longest = len(cell)
        px = max(70, min(220, 14 + longest * 7))
        col = TableColumn()
        col.Width = GridLength(px)
        tbl.Columns.Add(col)

    rg = TableRowGroup()
    tbl.RowGroups.Add(rg)

    for ri, row in enumerate(rows):
        is_header = (ri == 0)
        is_active = not is_header and any(_MARKER_START in cell for cell in row)
        if is_header:
            bg = _COL_TH_BG
        elif is_active:
            bg = _COL_TR_ACTIVE
        else:
            bg = _COL_TR_ALT if ri % 2 == 0 else _COL_TR_NORM

        tr = TableRow()
        tr.Background = bg
        for ci in range(col_count):
            cell_text = row[ci] if ci < len(row) else ""
            cell_text = cell_text.replace(_MARKER_START, "").replace(_MARKER_END, "")
            cell = TableCell()
            cell.BorderBrush = _COL_BORDER
            cell.BorderThickness = Thickness(0.5)
            cell.Padding = Thickness(6, 4, 6, 4)
            p = Paragraph()
            p.Margin = Thickness(0)
            if is_header:
                p.Foreground = _color(220, 240, 255)
                p.FontWeight = FontWeights.Bold
            elif is_active:
                p.Foreground = _GREEN_BRUSH
            else:
                p.Foreground = _COL_WHITE
            _apply_inline_to(p.Inlines, cell_text)
            cell.Blocks.Add(p)
            tr.Cells.Add(cell)
        rg.Rows.Add(tr)
    return tbl


def _build_wpf_flow_document(text):
    """Convert markdown text to a FlowDocument so the host RichTextBox can
    select+copy across the entire bubble."""
    doc = FlowDocument()
    doc.PagePadding = Thickness(0)
    doc.FontFamily = FontFamily("Segoe UI")
    doc.FontSize = 13
    doc.Foreground = _COL_WHITE
    doc.Background = Brushes.Transparent
    # Disable column layout — chat bubbles are narrow, multi-column flow would
    # collapse into a single skinny column with weird breaks.
    doc.IsColumnWidthFlexible = False
    doc.ColumnWidth = 1e6  # effectively single column

    # Strip outer ```markdown fence if Gemini wrapped the whole reply.
    _stripped = text.strip()
    if _stripped.startswith('```'):
        _first_nl = _stripped.find('\n')
        if _first_nl != -1:
            _opener = _stripped[3:_first_nl].strip().lower()
            _body = _stripped[_first_nl + 1:]
            if _body.endswith('```'):
                _body = _body[:-3].rstrip()
            if _opener in ('', 'markdown', 'md'):
                text = _body

    lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')

    i = 0
    while i < len(lines):
        line = lines[i]

        # ── Headings ──
        h3 = _re.match(r'^### (.+)', line)
        h2 = _re.match(r'^## (.+)', line)
        h1 = _re.match(r'^# (.+)', line)
        if h1:
            doc.Blocks.Add(_flow_paragraph(h1.group(1), fg=_COL_H1, size=16,
                                           bold=True, margin=Thickness(0, 6, 0, 2)))
            i += 1; continue
        if h2:
            doc.Blocks.Add(_flow_paragraph(h2.group(1), fg=_COL_H2, size=14,
                                           bold=True, margin=Thickness(0, 5, 0, 2)))
            i += 1; continue
        if h3:
            doc.Blocks.Add(_flow_paragraph(h3.group(1), fg=_COL_H3, size=13,
                                           bold=True, margin=Thickness(0, 4, 0, 1)))
            i += 1; continue

        # ── Horizontal rule ── (empty paragraph with bottom border via Section)
        if _re.match(r'^[-*_]{3,}$', line.strip()):
            sep_border = Border()
            sep_border.Height = 1
            sep_border.Background = _color(80, 80, 100)
            sep_border.Margin = Thickness(0, 4, 0, 4)
            bc = BlockUIContainer(sep_border)
            doc.Blocks.Add(bc)
            i += 1; continue

        # ── Table ──
        if _is_table_line(line):
            table_lines = []
            while i < len(lines) and _is_table_line(lines[i]):
                table_lines.append(lines[i])
                i += 1
            try:
                rows = _parse_table(table_lines)
                tbl = _build_flow_table(rows)
                if tbl is not None:
                    doc.Blocks.Add(tbl)
                else:
                    for tl in table_lines:
                        doc.Blocks.Add(_flow_paragraph(tl))
            except Exception as _e:
                import traceback as _tb
                _table_log("FLOW_TABLE_EXCEPTION", "{0}\n{1}".format(_e, _tb.format_exc()))
                for tl in table_lines:
                    doc.Blocks.Add(_flow_paragraph(tl))
            continue

        # ── Code block ──
        if line.strip().startswith('```'):
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith('```'):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing fence
            code_text = '\n'.join(code_lines)
            code_p = Paragraph()
            code_p.FontFamily = _MONO_FONT
            code_p.FontSize = 11
            code_p.Foreground = _COL_CODE
            code_p.Background = _color(20, 20, 30)
            code_p.Padding = Thickness(8, 6, 8, 6)
            code_p.Margin = Thickness(0, 3, 0, 3)
            code_p.BorderBrush = _color(70, 70, 100)
            code_p.BorderThickness = Thickness(1)
            code_p.Inlines.Add(Run(code_text))
            doc.Blocks.Add(code_p)
            continue

        # ── Bullet / numbered list ──
        bullet_m = _re.match(r'^(\s*)([-*+]|\d+[.)]) (.+)', line)
        if bullet_m:
            indent = len(bullet_m.group(1))
            marker = bullet_m.group(2)
            content = bullet_m.group(3)
            p = Paragraph()
            p.Margin = Thickness(indent * 8, 1, 0, 1)
            p.Foreground = _COL_WHITE
            marker_run = Run(u'▸ ' if not _re.match(r'\d', marker) else marker + ' ')
            marker_run.Foreground = _color(100, 180, 255)
            marker_run.FontWeight = FontWeights.Bold
            p.Inlines.Add(marker_run)
            _apply_inline_to(p.Inlines, content)
            doc.Blocks.Add(p)
            i += 1; continue

        # ── Blockquote ──
        if line.startswith('> '):
            p = Paragraph()
            p.Margin = Thickness(0, 2, 0, 2)
            p.Padding = Thickness(8, 2, 4, 2)
            p.BorderBrush = _color(100, 160, 255)
            p.BorderThickness = Thickness(3, 0, 0, 0)
            p.Foreground = _COL_MUTED
            p.FontStyle = FontStyles.Italic
            _apply_inline_to(p.Inlines, line[2:])
            doc.Blocks.Add(p)
            i += 1; continue

        # ── Empty line → small spacer paragraph ──
        if not line.strip():
            sp = Paragraph()
            sp.Margin = Thickness(0, 2, 0, 2)
            sp.FontSize = 4
            sp.Inlines.Add(Run(""))
            doc.Blocks.Add(sp)
            i += 1; continue

        # ── Plain paragraph ──
        doc.Blocks.Add(_flow_paragraph(line))
        i += 1

    return doc


def _make_selectable_richtextbox(document):
    """Wrap a FlowDocument in a read-only, transparent, borderless RichTextBox
    that allows native text selection + Ctrl+C copy."""
    rtb = RichTextBox()
    rtb.Document = document
    rtb.IsReadOnly = True
    rtb.IsReadOnlyCaretVisible = False
    rtb.IsDocumentEnabled = True   # required for clickable Hyperlinks inside read-only mode
    rtb.Background = Brushes.Transparent
    rtb.Foreground = _COL_WHITE
    rtb.BorderThickness = Thickness(0)
    rtb.Padding = Thickness(0)
    rtb.Margin = Thickness(0)
    rtb.IsTabStop = False
    rtb.FocusVisualStyle = None
    rtb.AcceptsTab = False
    rtb.AcceptsReturn = False
    # Block the built-in scrollbars — the outer ChatScroller handles scrolling;
    # an inner scrollbar would just chop bubble height and break layout.
    rtb.VerticalScrollBarVisibility = ScrollBarVisibility.Disabled
    rtb.HorizontalScrollBarVisibility = ScrollBarVisibility.Disabled
    return rtb


def _make_selectable_plain(text, foreground=None):
    """Build a read-only RichTextBox holding a single plain paragraph.
    Used for user bubbles and status-marker bubbles."""
    doc = FlowDocument()
    doc.PagePadding = Thickness(0)
    doc.FontFamily = FontFamily("Segoe UI")
    doc.FontSize = 13
    doc.Background = Brushes.Transparent
    doc.IsColumnWidthFlexible = False
    doc.ColumnWidth = 1e6
    p = Paragraph()
    p.Margin = Thickness(0)
    p.Foreground = foreground or _COL_WHITE
    _apply_inline_to(p.Inlines, text)
    doc.Blocks.Add(p)
    return _make_selectable_richtextbox(doc)


def _build_wpf_markdown(text):
    """Convert markdown text to a WPF StackPanel with styled elements."""
    panel = StackPanel()
    panel.Orientation = System.Windows.Controls.Orientation.Vertical

    # Gemini sometimes wraps the entire response in a ```markdown ... ``` fence.
    # That makes the code-block branch eat the whole document and the actual
    # markdown (including tables) renders as monospace text. Strip the outer
    # fence so the document below can be parsed normally.
    _stripped = text.strip()
    if _stripped.startswith('```'):
        _first_nl = _stripped.find('\n')
        if _first_nl != -1:
            _opener = _stripped[3:_first_nl].strip().lower()
            # Only strip if it's an explicit markdown fence (or bare ``` with
            # markdown-looking content). Don't touch code blocks like ```python.
            _body = _stripped[_first_nl + 1:]
            if _body.endswith('```'):
                _body = _body[:-3].rstrip()
            if _opener in ('', 'markdown', 'md'):
                text = _body

    lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')

    i = 0
    while i < len(lines):
        line = lines[i]

        # ── Headings ──
        h3 = _re.match(r'^### (.+)', line)
        h2 = _re.match(r'^## (.+)', line)
        h1 = _re.match(r'^# (.+)', line)
        if h1:
            tb = _make_tb(h1.group(1), fg=_COL_H1, size=16, bold=True)
            tb.Margin = Thickness(0, 6, 0, 2)
            panel.Children.Add(tb)
            i += 1; continue
        if h2:
            tb = _make_tb(h2.group(1), fg=_COL_H2, size=14, bold=True)
            tb.Margin = Thickness(0, 5, 0, 2)
            panel.Children.Add(tb)
            i += 1; continue
        if h3:
            tb = _make_tb(h3.group(1), fg=_COL_H3, size=13, bold=True)
            tb.Margin = Thickness(0, 4, 0, 1)
            panel.Children.Add(tb)
            i += 1; continue

        # ── Horizontal rule ──
        if _re.match(r'^[-*_]{3,}$', line.strip()):
            sep = Border()
            sep.Height = 1
            sep.Background = _color(80, 80, 100)
            sep.Margin = Thickness(0, 4, 0, 4)
            panel.Children.Add(sep)
            i += 1; continue

        # ── Table: collect contiguous table lines ──
        if _is_table_line(line):
            table_lines = []
            while i < len(lines) and _is_table_line(lines[i]):
                table_lines.append(lines[i])
                i += 1
            _table_log("BRANCH_TABLE", "collected={0}".format(len(table_lines)))
            try:
                rows = _parse_table(table_lines)
                grid = _build_table_grid(rows)
                if grid:
                    panel.Children.Add(grid)
                    _table_log("BRANCH_ADDED", "ok")
                else:
                    for tl in table_lines:
                        panel.Children.Add(_make_tb(tl))
                    _table_log("BRANCH_FALLBACK_NONE", "grid was None")
            except Exception as _e:
                import traceback as _tb
                _table_log("BRANCH_EXCEPTION", "{0}\n{1}".format(_e, _tb.format_exc()))
                for tl in table_lines:
                    panel.Children.Add(_make_tb(tl))
            continue

        # ── Code block (``` fenced) ──
        if line.strip().startswith('```'):
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith('```'):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip closing ```
            code_text = '\n'.join(code_lines)
            code_border = Border()
            code_border.Background = _color(20, 20, 30)
            code_border.BorderBrush = _color(70, 70, 100)
            code_border.BorderThickness = Thickness(1)
            code_border.CornerRadius = CornerRadius(4)
            code_border.Padding = Thickness(8, 6, 8, 6)
            code_border.Margin = Thickness(0, 3, 0, 3)
            tb = TextBlock()
            tb.Text = code_text
            tb.FontFamily = _MONO_FONT
            tb.FontSize = 11
            tb.Foreground = _COL_CODE
            tb.TextWrapping = TextWrapping.Wrap
            _enable_selection(tb)
            code_border.Child = tb
            panel.Children.Add(code_border)
            continue

        # ── Bullet / numbered list ──
        bullet_m = _re.match(r'^(\s*)([-*+]|\d+[.)]) (.+)', line)
        if bullet_m:
            indent = len(bullet_m.group(1))
            marker = bullet_m.group(2)
            content = bullet_m.group(3)

            # Use a Grid with Auto+* columns instead of a horizontal
            # StackPanel — StackPanels give children infinite width, so the
            # text TextBlock never wraps and runs off the right edge of the
            # chat window. The * column constrains the text to the available
            # width and TextWrapping.Wrap kicks in.
            item_grid = Grid()
            item_grid.Margin = Thickness(indent * 8, 1, 0, 1)
            item_grid.HorizontalAlignment = HorizontalAlignment.Stretch

            cd_marker = ColumnDefinition()
            cd_marker.Width = GridLength.Auto
            item_grid.ColumnDefinitions.Add(cd_marker)
            cd_text = ColumnDefinition()
            cd_text.Width = GridLength(1, GridUnitType.Star)
            item_grid.ColumnDefinitions.Add(cd_text)

            dot = TextBlock()
            dot.Text = u'▸ ' if not _re.match(r'\d', marker) else marker + ' '
            dot.Foreground = _color(100, 180, 255)
            dot.FontWeight = FontWeights.Bold
            dot.Margin = Thickness(0, 0, 4, 0)
            dot.VerticalAlignment = VerticalAlignment.Top
            Grid.SetColumn(dot, 0)
            item_grid.Children.Add(dot)

            tb = _make_tb(content)
            tb.TextWrapping = TextWrapping.Wrap
            Grid.SetColumn(tb, 1)
            item_grid.Children.Add(tb)

            panel.Children.Add(item_grid)
            i += 1; continue

        # ── Blockquote ──
        if line.startswith('> '):
            bq_border = Border()
            bq_border.BorderBrush = _color(100, 160, 255)
            bq_border.BorderThickness = Thickness(3, 0, 0, 0)
            bq_border.Padding = Thickness(8, 2, 4, 2)
            bq_border.Margin = Thickness(0, 2, 0, 2)
            tb = _make_tb(line[2:], fg=_COL_MUTED, italic=True)
            bq_border.Child = tb
            panel.Children.Add(bq_border)
            i += 1; continue

        # ── Empty line → small spacer ──
        if not line.strip():
            spacer = Border()
            spacer.Height = 4
            panel.Children.Add(spacer)
            i += 1; continue

        # ── Plain paragraph ──
        tb = _make_tb(line)
        tb.Margin = Thickness(0, 1, 0, 1)
        panel.Children.Add(tb)
        i += 1

    return panel


# Add reference for keyboard interop
clr.AddReference("WindowsFormsIntegration")
from System.Windows.Forms.Integration import ElementHost

# Persistent reference to prevent garbage collection of the chat window
_current_chat_window = None

_SPINNER_FRAMES = [u"⠋", u"⠙", u"⠹", u"⠸", u"⠼", u"⠴", u"⠦", u"⠧", u"⠇", u"⠏"]
_STATUS_PREFIX   = u"\x00STATUS\x00"


class AIChatWindow(object):
    """AI chat window for Gemini MCP (CPython/XamlReader compatible)."""
    def __init__(self, xaml_file):
        # Load the XAML root object
        stream = FileStream(xaml_file, FileMode.Open, FileAccess.Read, FileShare.Read)
        try:
            self.window = XamlReader.Load(stream)
        finally:
            stream.Close()

        self.history = []
        self.is_thinking = False
        self.cancelled = False
        self._uiapp = None

        # Spinner state
        self._spinner_border = None   # the live status bubble (or None when hidden)
        self._spinner_frame_tb = None # TextBlock holding the spinning char
        self._spinner_text_tb = None  # TextBlock holding the status text
        self._spinner_timer = None    # DispatcherTimer driving the animation
        self._spinner_frame_idx = 0

        self.setup_ui()

    def setup_ui(self):
        """Set up initial UI state."""
        # Find elements by name from the loaded window
        self.UserInput = self.window.FindName("UserInput")
        self.SendButton = self.window.FindName("SendButton")
        self.ChatHistory = self.window.FindName("ChatHistory")
        self.ChatScroller = self.window.FindName("ChatScroller")
        self.StopButton = self.window.FindName("StopButton")
        self.MenuButton = self.window.FindName("MenuButton")
        self.StatusIndicator = self.window.FindName("StatusIndicator")
        self.LetterA = self.window.FindName("LetterA")
        self.LetterI = self.window.FindName("LetterI")
        self.LetterD = self.window.FindName("LetterD")
        self.StatusText = self.window.FindName("StatusText")

        if self.UserInput:
            self.UserInput.Focus()
            self.UserInput.KeyDown += self.on_key_down
        if self.SendButton:
            self.SendButton.Click += self.on_send_click
        if self.StopButton:
            self.StopButton.Click += self.on_stop_click
        if self.MenuButton:
            self.MenuButton.Click += self.on_menu_click
            # ContextMenu is not in the visual tree; wire by index
            self.MenuButton.ContextMenu.Items[0].Click += self.on_clear_chat
            self.MenuButton.ContextMenu.Items[1].Click += self.on_clear_rag_cache
        if self.StatusIndicator:
            self.StatusIndicator.MouseLeftButtonUp += self.on_status_click

        # Status-indicator state. Poll thread runs every 30s by default but
        # accelerates to ~3s for the next tick whenever any agent's state
        # changes, so transitions (e.g. build finishes → A goes green) feel
        # snappy. The stop event lets us shut it down cleanly on window close.
        # _status_busy prevents overlapping refreshes (timer + user click).
        self._status_stop = threading.Event()
        self._status_busy = threading.Lock()
        self._status_thread = None
        self._status_next_interval = 30.0  # seconds; reset on each tick
        # Brushes used by the indicator (cached so we don't allocate per tick).
        self._brush_green = _color(76, 175, 80)    # online
        self._brush_amber = _color(255, 167, 38)   # busy/working
        self._brush_red = _color(229, 57, 53)      # offline
        self._brush_muted = _color(170, 170, 170)
        # Last known per-agent state ("green"/"amber"/"red"), used to detect
        # transitions and drive the fast-recovery cadence.
        self._agent_state = {"a": None, "i": None, "d": None}

        self.window.Closed += self._on_window_closed

    # ── Agent status indicator (AiD) ──────────────────────────────────────────
    #
    # Each agent has three states:
    #   green ("online")  — reachable and idle
    #   amber ("busy")    — reachable but actively working (mid-build, mid-call)
    #   red   ("offline") — unreachable or plumbing not initialized
    #
    # The poll runs every 30s normally, but accelerates to ~3s for the next
    # tick after any state change so transitions feel snappy. The first poll
    # is kicked off by main() AFTER init_bridge runs — not from window.Loaded,
    # because at Loaded the bridge isn't wired up yet and Agent A would
    # spuriously appear red until the first 30s tick.

    def start_status_poll(self):
        """Kick off the first status check and the 30s poll loop. Called by
        main() after init_bridge() has wired up the bridge plumbing."""
        if self._status_thread is not None:
            return  # already started
        # Diagnostic — verify FindName resolved every element. If any are None
        # the indicator can never update (the guards in _apply_status_to_ui
        # silently skip None elements), which would leave the XAML defaults
        # (all-green letters + "Checking agents...") stuck on screen.
        try:
            client.log("[status] wiring check: "
                       "StatusIndicator={} LetterA={} LetterI={} LetterD={} StatusText={}".format(
                           self.StatusIndicator is not None,
                           self.LetterA is not None,
                           self.LetterI is not None,
                           self.LetterD is not None,
                           self.StatusText is not None,
                       ))
        except Exception:
            pass
        self._refresh_status_async(is_click=False)
        self._status_thread = threading.Thread(target=self._status_poll_loop)
        self._status_thread.daemon = True
        self._status_thread.start()
        try:
            client.log("[status] poll loop started")
        except Exception:
            pass

    def _on_window_closed(self, sender, e):
        self._status_stop.set()

    def _status_poll_loop(self):
        """Sleep _status_next_interval, then refresh. Interval resets to 30s
        each tick unless the last tick detected a state change, in which case
        _refresh_status_async drops it to ~3s for one cycle (fast recovery)."""
        while not self._status_stop.wait(self._status_next_interval):
            try:
                self._refresh_status_async(is_click=False)
            except Exception as ex:
                client.log("[status] poll tick crashed: {}".format(ex))

    def _refresh_status_async(self, is_click):
        """Trigger a status refresh. Spawns three independent checker threads —
        one per agent — and updates each letter as soon as its check returns.

        Why parallel and incremental: the slowest check is Agent I's Railway
        probe, which can sit on a TCP connect for many seconds while the dyno
        wakes. If we waited for all three to complete before painting, the
        user would see "Checking agents..." for 30-50s on cold start every
        time Agent I was asleep. Now A and D show their real state within
        ~0.5-5s and I updates whenever Railway gets around to responding.

        `is_click=True` prewarms Agent I (wakes the Railway dyno) and
        surfaces a chat hint if Agent D stays offline.
        """
        if not self._status_busy.acquire(False):
            return  # another refresh already in flight; skip this tick

        if is_click:
            def _do_prewarm():
                try:
                    from revit_mcp.external_agents import _prewarm
                    _prewarm("agent_i")
                except Exception as ex:
                    client.log("[status] prewarm failed: {}".format(ex))
            tp = threading.Thread(target=_do_prewarm)
            tp.daemon = True
            tp.start()

        # Each checker writes its own slot in this dict so the finisher can
        # detect "all three done" and run change-detection / cadence logic.
        results = {"a": None, "i": None, "d": None}
        results_lock = threading.Lock()
        done_event = threading.Event()

        def _checker(key, evaluator):
            state = "red"
            try:
                state = evaluator()
            except Exception as ex:
                try:
                    client.log("[status] {} check crashed: {}".format(key, ex))
                except Exception:
                    pass
                state = "red"
            # Record the result and paint just this letter immediately.
            with results_lock:
                results[key] = state
                self._agent_state = dict(self._agent_state)
                self._agent_state[key] = state
                snapshot = dict(self._agent_state)
                all_done = all(v is not None for v in results.values())
            try:
                self.window.Dispatcher.BeginInvoke(
                    Action(lambda s=snapshot: self._apply_status_to_ui(s["a"], s["i"], s["d"]))
                )
            except Exception:
                pass
            if all_done:
                done_event.set()

        def _eval_a():
            return self._evaluate_agent_a()

        def _eval_i():
            from revit_mcp.external_agents import health_check, is_busy
            return self._evaluate_remote_agent("agent_i", health_check, is_busy)

        def _eval_d():
            from revit_mcp.external_agents import health_check, is_busy
            return self._evaluate_remote_agent("agent_d", health_check, is_busy)

        # Snapshot prev state for change detection before we overwrite it.
        prev_state = dict(self._agent_state)

        for key, evaluator in (("a", _eval_a), ("i", _eval_i), ("d", _eval_d)):
            t = threading.Thread(target=_checker, args=(key, evaluator))
            t.daemon = True
            t.start()

        def _finisher():
            try:
                # Hard cap so a hung check can't pin the busy lock forever.
                done_event.wait(15.0)
                with results_lock:
                    final = dict(self._agent_state)
                changed = (prev_state != final)
                any_red = any(v == "red" for v in final.values())
                self._status_next_interval = 2.0 if (changed or any_red) else 30.0
                try:
                    client.log("[status] tick: a={} i={} d={} changed={}".format(
                        final.get("a"), final.get("i"), final.get("d"), changed))
                except Exception:
                    pass
                if is_click and final.get("d") == "red":
                    msg = ("Agent D bridge not responding. If it didn't auto-start with Revit, "
                           "restart Revit, or click the **Bridge** button in the Agent D ribbon panel.")
                    try:
                        self.window.Dispatcher.BeginInvoke(
                            Action(lambda: self.add_message(msg, is_user=False))
                        )
                    except Exception:
                        pass
            finally:
                self._status_busy.release()

        tf = threading.Thread(target=_finisher)
        tf.daemon = True
        tf.start()

    def _evaluate_agent_a(self):
        """Return 'green', 'amber', or 'red' for Agent A.

        Strategy:
          - is_thinking is True       → amber (we're actively driving work)
          - bridge not initialized    → red
          - PING returns within 2s    → green
          - PING times out            → amber (bridge wired but busy/wedged)
                                         The bridge has a 1200s deadline with
                                         no per-call timeout, so we wrap the
                                         PING in a sacrificial thread + join.
        """
        try:
            if self.is_thinking:
                return "amber"
            from revit_mcp.bridge import is_bridge_initialized
            if not is_bridge_initialized():
                return "red"
        except Exception:
            return "red"

        result = {"pong": False, "error": False}

        def _do_ping():
            try:
                def ping_action():
                    return "PONG"
                res = bridge.mcp_event_handler.run_on_main_thread(ping_action)
                result["pong"] = (res == "PONG")
            except Exception:
                result["error"] = True

        t = threading.Thread(target=_do_ping)
        t.daemon = True
        t.start()
        # 5s is generous on purpose. The bridge's ExternalEvent dispatch timer
        # fires every 100ms but the actual Execute() runs only when Revit's
        # main thread is free; on first startup the main thread is still
        # finishing document load / other extensions' startup hooks, so the
        # very first PING after init_bridge can sit in the queue for several
        # seconds. 2s was producing false-amber on cold start.
        t.join(5.0)

        if result["pong"]:
            return "green"
        if result["error"]:
            return "red"
        # Timeout: queue is alive (we got here past is_bridge_initialized) but
        # something on the main thread is blocking the drain. That's "busy".
        return "amber"

    def _evaluate_remote_agent(self, agent_name, health_check, is_busy):
        """Return 'green'/'amber'/'red' for Agent I or Agent D.

        We treat 'busy' as authoritative: if we currently have an in-flight
        dispatch to this agent, it is by definition reachable AND working. No
        need to probe — and probing during a long call would just add load.
        """
        if is_busy(agent_name):
            return "amber"
        return "green" if health_check(agent_name) else "red"

    def _apply_status_to_ui(self, a_state, i_state, d_state):
        """Paint the letters and rewrite the status text. UI thread only.

        Any of the three states may be None when called from incremental
        per-agent updates — that agent's check is still in flight. In that
        case the letter is muted grey and the status text says "Checking
        Agent X...".
        """
        try:
            client.log("[status] apply: a={} i={} d={}".format(a_state, i_state, d_state))
        except Exception:
            pass
        brushes = {"green": self._brush_green,
                   "amber": self._brush_amber,
                   "red":   self._brush_red,
                   None:    self._brush_muted}
        if self.LetterA:
            self.LetterA.Foreground = brushes.get(a_state, self._brush_red)
        if self.LetterI:
            self.LetterI.Foreground = brushes.get(i_state, self._brush_red)
        if self.LetterD:
            self.LetterD.Foreground = brushes.get(d_state, self._brush_red)
        if self.StatusText:
            self.StatusText.Text = self._format_status_text(a_state, i_state, d_state)

    def _format_status_text(self, a_state, i_state, d_state):
        """Plain-English summary so users don't have to decode the colors.

        Priority: still-checking > working > offline > all online. We don't
        mix categories in one line — keeps it short and the most actionable
        signal wins. "Checking" beats everything because a None state means
        we genuinely don't know yet and shouldn't claim "all online".
        """
        # Still-checking — at least one of the per-agent threads hasn't
        # reported yet. Surface which one(s).
        checking = []
        if a_state is None:
            checking.append("Agent A")
        if i_state is None:
            checking.append("Agent I")
        if d_state is None:
            checking.append("Agent D")
        if checking:
            return "Checking {}...".format(self._join_agents(checking))

        working = []
        if a_state == "amber":
            working.append("Agent A")
        if i_state == "amber":
            working.append("Agent I")
        if d_state == "amber":
            working.append("Agent D")

        if working:
            return "{} {} working".format(
                self._join_agents(working),
                "is" if len(working) == 1 else "are",
            )

        offline = []
        if a_state == "red":
            offline.append("Agent A")
        if i_state == "red":
            offline.append("Agent I")
        if d_state == "red":
            offline.append("Agent D")

        if offline:
            if a_state == "red":
                # Without Agent A, the others are unreachable from here anyway.
                return "Agent A offline — chat unavailable"
            return "{} offline".format(self._join_agents(offline))

        return "All agents online"

    @staticmethod
    def _join_agents(names):
        """Oxford-comma join: ['A'] → 'A'; ['A','B'] → 'A and B';
        ['A','B','C'] → 'A, B, and C'."""
        if not names:
            return ""
        if len(names) == 1:
            return names[0]
        if len(names) == 2:
            return "{} and {}".format(names[0], names[1])
        return "{}, and {}".format(", ".join(names[:-1]), names[-1])

    def on_status_click(self, sender, e):
        """Manual refresh: re-evaluate everything, prewarm Agent I."""
        if self.StatusText:
            self.StatusText.Text = "Re-checking agents..."
        self._refresh_status_async(is_click=True)

    # ── Spinner bubble helpers ────────────────────────────────────────────────

    def _create_spinner_bubble(self):
        """Build and insert the persistent spinner bubble at the bottom of ChatHistory."""
        outer = Border()
        outer.Background = SolidColorBrush(Color.FromRgb(28, 28, 40))
        outer.CornerRadius = CornerRadius(8)
        outer.Padding = Thickness(12, 8, 12, 8)
        outer.HorizontalAlignment = HorizontalAlignment.Left
        outer.Margin = Thickness(0, 5, 10, 5)

        row = StackPanel()
        row.Orientation = System.Windows.Controls.Orientation.Horizontal

        frame_tb = TextBlock()
        frame_tb.Text = _SPINNER_FRAMES[0]
        frame_tb.Foreground = _color(100, 180, 255)
        frame_tb.FontSize = 14
        frame_tb.VerticalAlignment = VerticalAlignment.Center
        frame_tb.Margin = Thickness(0, 0, 8, 0)
        row.Children.Add(frame_tb)

        text_tb = TextBlock()
        text_tb.Text = u""
        text_tb.Foreground = _color(180, 180, 180)
        text_tb.FontSize = 13
        text_tb.VerticalAlignment = VerticalAlignment.Center
        text_tb.TextWrapping = TextWrapping.Wrap
        text_tb.MaxWidth = 340
        row.Children.Add(text_tb)

        outer.Child = row

        self._spinner_border = outer
        self._spinner_frame_tb = frame_tb
        self._spinner_text_tb = text_tb

        if self.ChatHistory:
            self.ChatHistory.Children.Add(outer)
        if self.ChatScroller:
            self.ChatScroller.ScrollToBottom()

        # Start the animation timer (100ms → ~10fps spin)
        timer = DispatcherTimer()
        timer.Interval = TimeSpan.FromMilliseconds(100)
        timer.Tick += self._on_spinner_tick
        timer.Start()
        self._spinner_timer = timer

    def _on_spinner_tick(self, sender, e):
        if self._spinner_frame_tb:
            self._spinner_frame_idx = (self._spinner_frame_idx + 1) % len(_SPINNER_FRAMES)
            self._spinner_frame_tb.Text = _SPINNER_FRAMES[self._spinner_frame_idx]

    def _update_spinner_text(self, text):
        """Update the status text inside the spinner bubble (or create it if absent)."""
        if self._spinner_border is None:
            self._create_spinner_bubble()
        if self._spinner_text_tb:
            self._spinner_text_tb.Text = text
        if self.ChatScroller:
            self.ChatScroller.ScrollToBottom()

    def _remove_spinner_bubble(self):
        """Stop the animation and remove the spinner bubble entirely."""
        if self._spinner_timer:
            self._spinner_timer.Stop()
            self._spinner_timer = None
        if self._spinner_border and self.ChatHistory:
            try:
                self.ChatHistory.Children.Remove(self._spinner_border)
            except Exception:
                pass
        self._spinner_border = None
        self._spinner_frame_tb = None
        self._spinner_text_tb = None

    def add_message(self, message, is_user=True):
        """Add a message to the chat history block."""
        new_border = Border()
        new_border.CornerRadius = CornerRadius(8)
        new_border.Padding = Thickness(12)

        if is_user:
            new_border.Background = SolidColorBrush(Color.FromRgb(0, 122, 204))
            new_border.HorizontalAlignment = HorizontalAlignment.Right
            new_border.Margin = Thickness(40, 5, 0, 5)
            new_border.MaxWidth = 420
        else:
            # Stretch to the chat column width so paragraph wrapping kicks in
            # at the actual window size — otherwise long lines push the bubble
            # past the right edge of the ChatScroller and get cropped.
            new_border.Background = SolidColorBrush(Color.FromRgb(38, 38, 52))
            new_border.HorizontalAlignment = HorizontalAlignment.Stretch
            new_border.Margin = Thickness(0, 5, 10, 5)

        if is_user:
            # User bubbles: read-only RichTextBox so the user can select +
            # copy their own prompt back. URLs in the prompt become clickable.
            new_border.Child = _make_selectable_plain(message, Brushes.White)
        elif _MARKER_START in message:
            # Progress/status messages with green/white colour markers
            doc = FlowDocument()
            doc.PagePadding = Thickness(0)
            doc.FontFamily = FontFamily("Segoe UI")
            doc.FontSize = 13
            doc.Background = Brushes.Transparent
            doc.IsColumnWidthFlexible = False
            doc.ColumnWidth = 1e6
            p = Paragraph()
            p.Margin = Thickness(0)
            p.Foreground = Brushes.White
            parts = _re.split(u'(|)', message)
            in_green = False
            for part in parts:
                if part == _MARKER_START:
                    in_green = True
                elif part == _MARKER_END:
                    in_green = False
                elif part:
                    def _factory(t, c=(_GREEN_BRUSH if in_green else Brushes.White)):
                        r = Run(t); r.Foreground = c; return r
                    _add_text_with_links_to(p.Inlines, part, _factory)
            doc.Blocks.Add(p)
            new_border.Child = _make_selectable_richtextbox(doc)
        else:
            # AI response — full markdown renderer (selectable RichTextBox)
            new_border.Child = _make_selectable_richtextbox(_build_wpf_flow_document(message))

        if self.ChatHistory:
            self.ChatHistory.Children.Add(new_border)
        if self.ChatScroller:
            self.ChatScroller.ScrollToBottom()

        # Add to history
        self.history.append({"text": message, "is_user": is_user})

    def on_menu_click(self, sender, e):
        self.MenuButton.ContextMenu.IsOpen = True

    def on_clear_chat(self, sender, e):
        self._remove_spinner_bubble()
        if self.ChatHistory:
            self.ChatHistory.Children.Clear()
        self.history = []
        self.add_message("Hello! I am your Gemini-powered Revit assistant. The MCP server is running and I'm ready to help.", is_user=False)

    def on_clear_rag_cache(self, sender, e):
        """Clear all caches that influence RAG / authority-code retrieval so the
        next build re-queries Vertex AI from scratch. Useful for demoing live RAG."""
        import os
        report_lines = []

        # 1. Disk chunk cache: %AppData%\Roaming\RevitMCP\cache\chunk_cache.json
        try:
            from revit_mcp.utils import get_appdata_path
            chunk_path = os.path.join(get_appdata_path("cache"), "chunk_cache.json")
            if os.path.isfile(chunk_path):
                os.remove(chunk_path)
                report_lines.append("- Deleted disk chunk cache (`chunk_cache.json`)")
            else:
                report_lines.append("- Disk chunk cache was already empty")
        except Exception as ex:
            report_lines.append("- Disk chunk cache: failed to delete ({})".format(ex))

        # 2. In-memory chunk cache held inside sub_agent module
        try:
            from revit_mcp.agents import sub_agent
            sub_agent._chunk_cache = {}
            report_lines.append("- Cleared in-memory chunk cache")
        except Exception as ex:
            report_lines.append("- In-memory chunk cache: failed to clear ({})".format(ex))

        # 3. In-memory rag_rules cache on the orchestrator singleton
        try:
            from revit_mcp.dispatcher import orchestrator
            orchestrator._rag_cache = {}
            report_lines.append("- Cleared orchestrator RAG cache")
        except Exception as ex:
            report_lines.append("- Orchestrator RAG cache: failed to clear ({})".format(ex))

        # 3b. Disk RAG rules cache: %AppData%\Roaming\RevitMCP\cache\rag_rules_cache.json
        # (synthesised rules that survive Revit restarts so a "30-storey office"
        # rebuild after a restart doesn't re-pay the ~50s RAG cost)
        try:
            from revit_mcp.utils import get_appdata_path
            rules_path = os.path.join(get_appdata_path("cache"), "rag_rules_cache.json")
            if os.path.isfile(rules_path):
                os.remove(rules_path)
                report_lines.append("- Deleted disk RAG rules cache (`rag_rules_cache.json`)")
            else:
                report_lines.append("- Disk RAG rules cache was already empty")
        except Exception as ex:
            report_lines.append("- Disk RAG rules cache: failed to delete ({})".format(ex))

        # 4. Per-option compliance snapshots in build_options.json — these take
        # priority over the caches above (dispatcher.py:205) and would otherwise
        # cause the next build to replay the previous run's RAG verbatim.
        try:
            from revit_mcp.build_memory import get_options_manager
            mgr = get_options_manager()
            mgr._ensure_loaded()
            stripped = 0
            for opt in mgr._data.get("options", []):
                if opt.get("rag_rules") or opt.get("compliance_snapshot"):
                    stripped += 1
                opt["rag_rules"] = None
                opt["compliance_snapshot"] = ""
                for rev in opt.get("revisions", []) or []:
                    if rev.get("rag_rules") or rev.get("compliance_snapshot"):
                        stripped += 1
                    rev["rag_rules"] = None
                    rev["compliance_snapshot"] = ""
            mgr._save()
            report_lines.append(
                "- Stripped saved compliance from `build_options.json` ({} entries cleared)".format(stripped))
        except Exception as ex:
            report_lines.append("- Saved compliance in build_options.json: failed to clear ({})".format(ex))

        msg = "RAG cache cleared. The next build will fetch fresh data from Vertex AI.\n\n" + "\n".join(report_lines)
        self.add_message(msg, is_user=False)
        client.log("UI: RAG cache cleared via menu — " + " | ".join(report_lines))

    def on_send_click(self, sender, e):
        """Handle the send button click."""
        try:
            if not self.UserInput: return
            user_text = self.UserInput.Text.strip()
            if user_text:
                if user_text.lower() == "ping":
                    self.UserInput.Text = ""
                    self.add_message("ping", is_user=True)
                    self.test_bridge()
                    return

                self.UserInput.Text = ""
                self.add_message(user_text, is_user=True)

                self.is_thinking = True
                self.cancelled = False
                if self.StopButton:
                    import System.Windows
                    self.StopButton.Visibility = System.Windows.Visibility.Visible
                if self.SendButton:
                    self.SendButton.IsEnabled = False

                # Create the spinner bubble immediately so the user sees activity right away
                self._create_spinner_bubble()
                self._update_spinner_text(u"Thinking...")

                client.log("UI Thread: Dispatching prompt: " + user_text[:30])
                thread = threading.Thread(target=self.get_gemini_response, args=(user_text,))
                thread.daemon = True
                thread.start()
                client.log("UI Thread: Thread.start() called.")
        except Exception as ex:
            import traceback
            from pyrevit import forms
            forms.alert("UI Error: {}\n\n{}".format(str(ex), traceback.format_exc()))

    def on_stop_click(self, sender, e):
        """Handle the stop button click."""
        self.cancelled = True
        self.is_thinking = False
        self._remove_spinner_bubble()
        if self.StopButton:
            import System.Windows
            self.StopButton.Visibility = System.Windows.Visibility.Collapsed
        if self.SendButton:
            self.SendButton.IsEnabled = True
        self.add_message("Operation cancelled by user.", is_user=False)

    def get_gemini_response(self, prompt):
        responded = False
        try:
            client.log("Background Thread: Starting orchestration...")

            if not bridge._uiapp:
                client.log("Background Thread Error: bridge._uiapp is None.")
                err = "Error: Revit connection lost. Please click 'Start Server' again."
                from System import Action
                self.window.Dispatcher.BeginInvoke(Action(lambda: self.on_response_finished(err)))
                return

            from revit_mcp.progress_tracker import BuildProgressTracker
            tracker = BuildProgressTracker(callback=self.update_progress)
            response = orchestrator.run_full_stack(bridge._uiapp, prompt, tracker=tracker, history=self.history)
            responded = True

            client.log("UI Thread: Response received.")
            from System import Action
            self.window.Dispatcher.BeginInvoke(Action(lambda: self.on_response_finished(response)))

        except Exception as e:
            import traceback
            err_msg = "UI Thread CRASH: {}: {}\n{}".format(type(e).__name__, str(e), traceback.format_exc())
            # Belt-and-braces: log via client, also dump to a dedicated crash log
            # at %APPDATA%\RevitMCP\logs\ui_thread_crash.log so we never lose the
            # trace even if client.log itself is the thing that broke.
            try: client.log(err_msg)
            except: pass
            try:
                import os as _os
                from revit_mcp.utils import get_appdata_path
                _crash_path = _os.path.join(get_appdata_path("logs"), "ui_thread_crash.log")
                with open(_crash_path, "a", encoding="utf-8") as _f:
                    import datetime as _dt
                    _f.write("\n[{}] {}\n".format(_dt.datetime.now().isoformat(), err_msg))
            except Exception:
                pass
            print(err_msg)
            from System import Action
            # Surface the exception type+message in the chat so the user can see
            # what went wrong without having to dig in the log.
            _user_msg = "Error: {}: {}".format(type(e).__name__, str(e))[:500]
            self.window.Dispatcher.BeginInvoke(Action(lambda m=_user_msg: self.on_response_finished(m)))
        finally:
            if not responded and not self.cancelled:
                from System import Action
                self.window.Dispatcher.BeginInvoke(Action(lambda: self.on_response_finished("Error: Request failed.")))

    def on_response_finished(self, response):
        """Remove spinner, re-enable input, then show the final response bubble."""
        if self.cancelled: return
        self.is_thinking = False
        self._remove_spinner_bubble()
        if self.StopButton:
            import System.Windows
            self.StopButton.Visibility = System.Windows.Visibility.Collapsed
        if self.SendButton:
            self.SendButton.IsEnabled = True
        self._insert_permanent_bubble(response)
        if response.startswith("Error:") and self.ChatHistory and self.ChatHistory.Children.Count > 0:
            last_border = self.ChatHistory.Children[self.ChatHistory.Children.Count - 1]
            last_border.Background = SolidColorBrush(Color.FromRgb(178, 34, 34))

    def update_progress(self, msg):
        """Thread-safe callback from BuildProgressTracker.
        STATUS messages update the spinner text; everything else appends a permanent bubble.
        """
        from System import Action
        def _apply():
            if self.cancelled or not self.is_thinking:
                return
            if msg.startswith(_STATUS_PREFIX):
                status_text = msg[len(_STATUS_PREFIX):]
                if status_text:
                    self._update_spinner_text(status_text)
                else:
                    # Empty status = stop signal, spinner will be removed by on_response_finished
                    pass
            else:
                # Permanent message — append a new bubble above the spinner
                self._insert_permanent_bubble(msg)
            if _has_doevents:
                try:
                    WinForms.DoEvents()
                except:
                    pass

        if self.window.Dispatcher.CheckAccess():
            _apply()
        else:
            self.window.Dispatcher.Invoke(Action(_apply))

    def _insert_permanent_bubble(self, text):
        """Add a permanent AI bubble just above the spinner bubble."""
        new_border = Border()
        new_border.Background = SolidColorBrush(Color.FromRgb(38, 38, 52))
        new_border.CornerRadius = CornerRadius(8)
        new_border.Padding = Thickness(12)
        new_border.HorizontalAlignment = HorizontalAlignment.Stretch
        new_border.Margin = Thickness(0, 5, 10, 5)
        new_border.Child = _make_selectable_richtextbox(_build_wpf_flow_document(text))

        if self.ChatHistory:
            if self._spinner_border is not None:
                # Insert just before the spinner so spinner stays at the bottom
                idx = self.ChatHistory.Children.IndexOf(self._spinner_border)
                if idx >= 0:
                    self.ChatHistory.Children.Insert(idx, new_border)
                else:
                    self.ChatHistory.Children.Add(new_border)
            else:
                self.ChatHistory.Children.Add(new_border)

        self.history.append({"text": text, "is_user": False})
        if self.ChatScroller:
            self.ChatScroller.ScrollToBottom()

    def on_key_down(self, sender, e):
        """Handle Enter key press to send message."""
        if e.Key == System.Windows.Input.Key.Enter:
            self.on_send_click(sender, e)

    def test_bridge(self):
        """Diagnostic tool to verify Revit bridge health - run in thread to avoid UI hang."""
        def run_test():
            try:
                client.log("Background Thread: Manual Bridge Ping started.")
                def ping_action(): return "PONG"
                # This now waits for the Timer to pick it up
                res = bridge.mcp_event_handler.run_on_main_thread(ping_action)

                client.log("Background Thread: Bridge Ping SUCCESS: " + str(res))
                self.window.Dispatcher.BeginInvoke(Action(lambda: self.add_message("Bridge Status: ACTIVE (PONG received)", is_user=False)))
            except Exception as e:
                import traceback
                err = "Bridge Ping FAILED: {}\n{}".format(str(e), traceback.format_exc())
                client.log(err)
                self.window.Dispatcher.BeginInvoke(Action(lambda: self.add_message("Bridge Status: OFFLINE\n" + str(e), is_user=False)))

        t = threading.Thread(target=run_test)
        t.daemon = True
        t.start()

    # Proxy methods for the main loop
    def Show(self, uiapp):
        # Set Revit as the owner of the window to allow keyboard input
        self._uiapp = uiapp
        helper = WindowInteropHelper(self.window)
        helper.Owner = uiapp.MainWindowHandle

        try:
            ElementHost.EnableModelessKeyboardInterop(self.window)
        except:
            pass

        self.window.Show()
        self.window.Activate()

        # START THE IDLING PUMP (Provides valid API context for Transactions)
        try:
            self._uiapp.Idling += bridge.idling_handler
            client.log("UI: Idling Pump subscribed (v9-FINAL).")
        except Exception as e:
            client.log("UI: Idling subscription error: " + str(e))

    def Close(self):
        try:
            if self._uiapp:
                self._uiapp.Idling -= bridge.idling_handler
                client.log("UI: Idling Pump unsubscribed.")
        except:
            pass
        self.window.Close()

    @property
    def Visibility(self): return self.window.Visibility


def pump():
    """Pump Windows messages to keep STA message queue clear."""
    if _has_doevents:
        try:
            WinForms.DoEvents()
        except:
            pass

def main():
    # We DO NOT delete modules starting with 'revit_mcp' here.
    # This ensures the background thread and this UI script share the same module objects.
    output = script.get_output()
    output.close_others(all_open_outputs=True)

    try:
        from revit_mcp.runner import start_mcp_server
        success = start_mcp_server()
    except Exception as e:
        import traceback
        output.print_md("## Failed to Start")
        output.print_md("**Error:** `{}`\n```\n{}\n```".format(str(e), traceback.format_exc()))
        return

    if not success:
        output.print_md("## Server already active on Port 8001.")
    else:
        output.print_md("## Gemini MCP Server: Active")
        output.print_md("- **Port:** 8001")
        output.print_md("- **Inspector:** `http://localhost:8001/sse`")
        output.print_md("- **DoEvents pump:** `{}`".format("Active" if _has_doevents else "Unavailable"))

    output.print_md("---")
    output.print_md("**Minimize** this window - do NOT close it (it keeps the server alive).")

    uiapp = HOST_APP.uiapp

    if not _init_success:
        output.print_md("## Plugin components failed to load.")
        output.print_md("Check your installation and try clicking 'Start Server' again.")
        output.print_md("---")
        output.print_md("### Error Details:")
        output.print_md("```\n{}\n```".format(_init_error))
        return

    # Initialize and show the chat window
    # Cleanup OLD windows if they exist
    global _current_chat_window
    if _current_chat_window:
        try:
            from revit_mcp.gemini_client import client
            client.log("Closing existing Chat Window...")
            _current_chat_window.Close()
        except Exception as e:
            import traceback
            from pyrevit import forms
            forms.alert("Error closing existing chat window: {}\n\n{}".format(str(e), traceback.format_exc()))

    xaml_file = os.path.join(os.path.dirname(__file__), "chat_ui.xaml")
    _current_chat_window = AIChatWindow(xaml_file)
    _current_chat_window.Show(uiapp)

    # ── External-agent health check (Agent D and any future registered agents) ──
    # Runs on a background thread so it never blocks the chat from opening.
    # Posts a one-line hint into the chat for any agent whose bridge isn't
    # listening yet. The agent_d intent itself still works without this — it
    # just falls back to the "couldn't reach" error if the user tries to use
    # an offline agent. This is purely a helpful nag.
    def _external_agents_startup_probe():
        try:
            from revit_mcp.external_agents import startup_report
            from revit_mcp.gemini_client import client as _gc
            results = startup_report(tracker_callback=lambda line: _gc.log("[startup probe] " + line))
            offline = [r for r in results if not r["ok"]]
            if not offline:
                return
            lines = ["**Tip:** these external agents aren't running yet. Click their ribbon button (or restart Revit if auto-start is configured) to enable them:"]
            for r in offline:
                lines.append("- {} ({})".format(r["display_name"], r["name"]))
            msg = "\n".join(lines)
            try:
                _current_chat_window.window.Dispatcher.BeginInvoke(
                    Action(lambda: _current_chat_window.add_message(msg, is_user=False))
                )
            except Exception as _e:
                _gc.log("[startup probe] failed to post nag: {}".format(_e))
        except Exception as _e:
            try:
                from revit_mcp.gemini_client import client as _gc
                _gc.log("[startup probe] crashed: {}".format(_e))
            except Exception:
                pass

    import threading as _t
    _t.Thread(target=_external_agents_startup_probe, daemon=True).start()

    # INITIALIZE BRIDGE (CRITICAL for non-blocking UI)
    from revit_mcp.bridge import init_bridge
    init_bridge(uiapp)

    # Start the agent status poll only AFTER init_bridge — at window.Loaded
    # time the bridge isn't wired yet, so the first PING would always fail
    # and Agent A would show red for up to 30s before the next tick fixes it.
    try:
        _current_chat_window.start_status_poll()
    except Exception as _e:
        client.log("[status] failed to start poll: {}".format(_e))

    output.print_md("**UI Active.** (Keep this window open to maintain AI connection)")
    output.print_md("---")
    output.print_md("> You can now use the Chat Window while Revit is running. The server will process your requests in the background.")

if __name__ == '__main__':
    main()
