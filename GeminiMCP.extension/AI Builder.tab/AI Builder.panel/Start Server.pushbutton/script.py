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
import System.Windows.Input
import threading
from System.Windows import Window, HorizontalAlignment, VerticalAlignment, Thickness, CornerRadius, TextWrapping, GridLength, GridUnitType
from System.Windows.Media import Brushes, Color, SolidColorBrush, FontFamily
from System.Windows.Controls import Border, TextBox, TextBlock, StackPanel, Grid, ColumnDefinition, RowDefinition, ScrollViewer, ScrollBarVisibility
from System.Windows.Documents import Run, Bold, Italic, Underline
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


def _apply_inline(tb, text):
    """Parse **bold**, *italic*, `code`, and plain text into TextBlock Inlines."""
    pattern = _re.compile(r'(\*\*(.+?)\*\*|\*(.+?)\*|`([^`]+?)`)')
    pos = 0
    for m in pattern.finditer(text):
        if m.start() > pos:
            tb.Inlines.Add(Run(text[pos:m.start()]))
        full = m.group(0)
        if full.startswith('**'):
            r = Run(m.group(2))
            r.FontWeight = FontWeights.Bold
            tb.Inlines.Add(r)
        elif full.startswith('*'):
            r = Run(m.group(3))
            r.FontStyle = FontStyles.Italic
            tb.Inlines.Add(r)
        elif full.startswith('`'):
            r = Run(m.group(4))
            r.FontFamily = _MONO_FONT
            r.Foreground = _COL_CODE
            tb.Inlines.Add(r)
        pos = m.end()
    if pos < len(text):
        tb.Inlines.Add(Run(text[pos:]))


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
    return tb


def _is_table_line(line):
    return '|' in line


def _parse_table(lines):
    rows = []
    for ln in lines:
        ln = ln.strip()
        if not ln or _re.match(r'^\|[-| :]+\|$', ln):
            continue
        cells = [c.strip() for c in ln.strip('|').split('|')]
        rows.append(cells)
    return rows


def _build_table_grid(rows):
    if not rows:
        return None

    col_count = max(len(r) for r in rows)
    grid = Grid()
    grid.Margin = Thickness(0, 4, 0, 4)

    for _ in range(col_count):
        cd = ColumnDefinition()
        cd.Width = GridLength(1, GridUnitType.Star)
        grid.ColumnDefinitions.Add(cd)

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

            tb = _make_tb(cell_text, bold=is_header, wrap=True)
            if is_header:
                tb.Foreground = _color(220, 240, 255)
            elif is_active:
                tb.Foreground = _GREEN_BRUSH
            cell_border.Child = tb

            Grid.SetRow(cell_border, ri)
            Grid.SetColumn(cell_border, ci)
            grid.Children.Add(cell_border)

    return grid


def _build_wpf_markdown(text):
    """Convert markdown text to a WPF StackPanel with styled elements."""
    panel = StackPanel()
    panel.Orientation = System.Windows.Controls.Orientation.Vertical

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
            rows = _parse_table(table_lines)
            grid = _build_table_grid(rows)
            if grid:
                panel.Children.Add(grid)
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
            code_border.Child = tb
            panel.Children.Add(code_border)
            continue

        # ── Bullet / numbered list ──
        bullet_m = _re.match(r'^(\s*)([-*+]|\d+[.)]) (.+)', line)
        if bullet_m:
            indent = len(bullet_m.group(1))
            marker = bullet_m.group(2)
            content = bullet_m.group(3)
            item_panel = StackPanel()
            item_panel.Orientation = System.Windows.Controls.Orientation.Horizontal
            item_panel.Margin = Thickness(indent * 8, 1, 0, 1)
            dot = TextBlock()
            dot.Text = u'▸ ' if not _re.match(r'\d', marker) else marker + ' '
            dot.Foreground = _color(100, 180, 255)
            dot.FontWeight = FontWeights.Bold
            dot.Margin = Thickness(0, 0, 4, 0)
            dot.VerticalAlignment = VerticalAlignment.Top
            item_panel.Children.Add(dot)
            tb = _make_tb(content)
            item_panel.Children.Add(tb)
            panel.Children.Add(item_panel)
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
            new_border.Background = SolidColorBrush(Color.FromRgb(38, 38, 52))
            new_border.HorizontalAlignment = HorizontalAlignment.Left
            new_border.Margin = Thickness(0, 5, 10, 5)

        if is_user:
            # User bubbles: plain TextBlock
            txt = TextBlock()
            txt.Text = message
            txt.TextWrapping = TextWrapping.Wrap
            txt.Foreground = Brushes.White
            new_border.Child = txt
        elif _MARKER_START in message:
            # Progress/status messages with green/white colour markers
            txt = TextBlock()
            txt.TextWrapping = TextWrapping.Wrap
            txt.Foreground = Brushes.White
            parts = _re.split(u'(|)', message)
            in_green = False
            for part in parts:
                if part == _MARKER_START:
                    in_green = True
                elif part == _MARKER_END:
                    in_green = False
                elif part:
                    run = Run(part)
                    run.Foreground = _GREEN_BRUSH if in_green else Brushes.White
                    txt.Inlines.Add(run)
            new_border.Child = txt
        else:
            # AI response — full markdown renderer
            new_border.Child = _build_wpf_markdown(message)

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
            err_msg = "UI Thread CRASH: {}\n{}".format(str(e), traceback.format_exc())
            try: client.log(err_msg)
            except: pass
            print(err_msg)
            from System import Action
            self.window.Dispatcher.BeginInvoke(Action(lambda: self.on_response_finished("Error: check log for details.")))
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
        new_border.HorizontalAlignment = HorizontalAlignment.Left
        new_border.Margin = Thickness(0, 5, 10, 5)
        new_border.Child = _build_wpf_markdown(text)

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

    # INITIALIZE BRIDGE (CRITICAL for non-blocking UI)
    from revit_mcp.bridge import init_bridge
    init_bridge(uiapp)

    output.print_md("**UI Active.** (Keep this window open to maintain AI connection)")
    output.print_md("---")
    output.print_md("> You can now use the Chat Window while Revit is running. The server will process your requests in the background.")

if __name__ == '__main__':
    main()
