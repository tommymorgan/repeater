#!/usr/bin/env python3
"""Native, local-only The Repeater desktop. Audio starts only on explicit Record."""

import sys
import signal
import threading
import time
import hashlib
import json
from dataclasses import replace
from pathlib import Path

from identity import APP_ID, APP_NAME
try:
    import gi
    gi.require_version('Gtk', '4.0')
    gi.require_version('Adw', '1')
    from gi.repository import Adw, Gio, GLib, Gtk, Pango
except (ImportError, ValueError) as error:
    print(f'{APP_NAME} needs Python PyGObject, GTK 4 and libadwaita. Install your distribution’s python-gobject, gtk4 and libadwaita packages, then run python3 linux/gui.py.\n{error}', file=sys.stderr)
    raise SystemExit(1)

from capture import list_devices
from desktop import (DesktopController, DesktopError, Preferences, list_meetings,
                     load_preferences, save_preferences,
                     settings_ready, _validate_preferences, set_archived,
                     trash_meeting, restore_trashed_meeting, PREFERENCE_PATHS)
from transcribe import (render_exports, load_transcript_edit_state, save_transcript_edit_state,
                        edit_transcript_text, undo_transcript_edit, redo_transcript_edit,
                        rename_speaker, reassign_segment, confirm_segment, speaker_label,
                        reject_segment, create_speaker_and_reassign,
                        PROFILE_DEFAULTS)
from transcript import TranscriptPane
from playback import SegmentPlayer, PlaybackError
from review import (ReviewConfig, load_review, run_review, review_findings,
                    record_decision, record_playback, excerpt_interval)
from voices import VoiceStore, VoiceError, embed_windows, windows_for_segment

PROFILE_CHOICES = (None, *PROFILE_DEFAULTS)


def label(text, style=None):
    widget = Gtk.Label(label=text, xalign=0, wrap=True)
    if style:
        widget.add_css_class(style)
    return widget


def button(text, callback, icon=None):
    widget = Gtk.Button(label=text) if icon is None else Gtk.Button(icon_name=icon)
    widget.set_tooltip_text(text)
    widget.update_property([Gtk.AccessibleProperty.LABEL], [text])
    widget.connect('clicked', callback)
    return widget


def duration(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f'{hours:02}:{minutes:02}:{seconds:02}' if hours else f'{minutes:02}:{seconds:02}'


def recording_status_icon(failed, transcribed):
    if failed:
        return Gtk.Image.new_from_icon_name('dialog-warning-symbolic')
    if transcribed:
        icon = Gtk.Overlay()
        icon.set_child(Gtk.Image.new_from_icon_name('x-office-document-symbolic'))
        check = Gtk.Image.new_from_icon_name('emblem-ok-symbolic')
        check.set_pixel_size(9)
        check.set_halign(Gtk.Align.END)
        check.set_valign(Gtk.Align.END)
        icon.add_overlay(check)
        return icon
    # No standard waveform icon is guaranteed by the freedesktop icon theme.
    icon = Gtk.DrawingArea(content_width=16, content_height=16)
    icon.set_valign(Gtk.Align.CENTER)
    def draw(widget, context, width, height):
        color = widget.get_style_context().get_color()
        context.set_source_rgba(color.red, color.green, color.blue, color.alpha)
        context.set_line_width(1.5)
        context.set_line_cap(1)
        for index, amplitude in enumerate((3, 7, 12, 7, 4)):
            x = width / 2 + (index - 2) * 3
            context.move_to(x, (height - amplitude) / 2)
            context.line_to(x, (height + amplitude) / 2)
        context.stroke()
    icon.set_draw_func(draw)
    return icon


class RepeaterWindow(Adw.ApplicationWindow):
    def __init__(self, application):
        super().__init__(application=application, title=APP_NAME, default_width=1080,
                         default_height=820)
        self.preferences = Preferences()
        self.collection = False
        self.edit_states = {}
        self.edit_dirty = set()
        self.edit_save_inflight = False
        self.edit_save_source = 0
        self.edit_save_failed = False
        self.transcript_loading = False
        self.edit_transition = None
        self.last_edit = None
        self.record_errors = {}
        self.recovery_actions = {}
        self.inference_ready = False
        self.inference_preferences = None
        self.player = SegmentPlayer(self._playback_changed)
        self.review_mode = False
        self.review_store = None
        self.review_generation = 0
        self.review_scanning = False
        self.review_cancel = None
        self.review_playback = None
        self.review_error = None
        self.review_writes = []
        self.review_write_active = False
        self.review_close_pending = False
        self.selected_meeting = None
        self.meetings = []
        self.devices = {'microphones': [], 'outputs': []}
        self.device_names = {'microphones': [], 'outputs': []}
        self.closing = False
        self.pending_action = False
        self.history_generation = 0
        self.transcript_generation = 0
        self.voices = VoiceStore(Path(GLib.get_user_data_dir()) / 'parrot' / 'voice-profiles.json')
        self.voice_hints = {}
        self.voice_generation = 0
        self.voice_offer_counts = {}
        self.voice_offer_dialog = None
        self.speaker_panel_open = False
        self.speaker_open_id = None
        self.speaker_rename_id = None
        self.speaker_correction_index = None
        self.completed_session = None
        self.refresh_transcripts = set()
        self.last_snapshot = None
        self.shutdown_complete = False
        self.settings_page = None
        self.autosave_source = 0
        self.save_inflight = False
        self.save_pending = None
        self.saved_preferences = None
        self.preference_revision = 0
        self.settings_dirty = False
        self.settings_initializing = False
        self.close_after_save = False
        self.refreshing_history = False
        self.controller = DesktopController(self._controller_changed)
        self.connect('close-request', self._close_requested)
        self._build()
        GLib.timeout_add_seconds(1, self._tick)
        self._run(load_preferences, self._preferences_loaded)
        self.refresh_history()
        self.refresh_devices()
        self._apply_snapshot(self.controller.snapshot)

    def _run(self, work, done=None, failed=None):
        def execute():
            try:
                result = work()
            except Exception as error:
                GLib.idle_add(failed or self.show_error, str(error))
            else:
                if done:
                    GLib.idle_add(done, result)
        threading.Thread(target=execute, daemon=True).start()

    def _build(self):
        self.layout = Gtk.Stack(hhomogeneous=False, vhomogeneous=False)
        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(self.layout)
        self.set_content(self.toast_overlay)
        self.split = Adw.NavigationSplitView(collapsed=True)
        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.paned.set_position(360)
        self.paned.set_resize_start_child(False)
        self.paned.set_shrink_start_child(False)
        self.paned.set_shrink_end_child(False)
        self.layout.add_named(self.paned, 'wide')
        self.layout.add_named(self.split, 'compact')
        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse('max-width: 760sp'))
        breakpoint.connect('apply', lambda *_: self._set_compact(True))
        breakpoint.connect('unapply', lambda *_: self._set_compact(False))
        self.add_breakpoint(breakpoint)
        sidebar = Adw.ToolbarView()
        sidebar_header = Adw.HeaderBar(show_end_title_buttons=False)
        self.sidebar_title = Gtk.Label(label='Recordings')
        self.sidebar_heading = Gtk.Stack()
        self.sidebar_heading.add_named(self.sidebar_title, 'title')
        self.speaker_back = button('Back to Recordings', lambda *_: self._close_speakers())
        self.speaker_back.set_child(Adw.ButtonContent(icon_name='go-previous-symbolic', label='Back to Recordings'))
        self.sidebar_heading.add_named(self.speaker_back, 'back')
        sidebar_header.set_title_widget(self.sidebar_heading)
        self.history_refresh = button('Refresh history', lambda *_: self.refresh_history(), 'view-refresh-symbolic')
        sidebar_header.pack_end(self.history_refresh)
        sidebar.add_top_bar(sidebar_header)
        history_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        history_box.set_margin_top(12)
        history_box.set_margin_bottom(12)
        history_box.set_margin_start(12)
        history_box.set_margin_end(12)
        self.new_button = button('New recording', lambda *_: self.show_recorder(), 'list-add-symbolic')
        self.new_button.set_label('New recording')
        history_box.append(self.new_button)
        collections = Gtk.Box(homogeneous=True)
        collections.add_css_class('linked')
        self.active_collection = Gtk.ToggleButton(label='Active 0')
        self.archived_collection = Gtk.ToggleButton(label='Archived 0')
        self.archived_collection.set_group(self.active_collection)
        for archived, widget in ((False, self.active_collection), (True, self.archived_collection)):
            widget.connect('toggled', self._collection_changed, archived)
            collections.append(widget)
        self.active_collection.set_active(True)
        history_box.append(collections)
        self.history_empty = label('No recordings yet. Start a recording to save it here.', 'dim-label')
        history_box.append(self.history_empty)
        self.history_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.history_list.add_css_class('navigation-sidebar')
        self.history_list.connect('row-selected', self._meeting_selected)
        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(self.history_list)
        history_box.append(scroller)
        self.sidebar_stack = Gtk.Stack(vexpand=True)
        self.sidebar_stack.add_named(history_box, 'recordings')
        self.speaker_scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.speaker_cards = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                                     margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        self.speaker_scroll.set_child(self.speaker_cards)
        self.sidebar_stack.add_named(self.speaker_scroll, 'speakers')
        sidebar.set_content(self.sidebar_stack)
        self.sidebar_page = Adw.NavigationPage.new(sidebar, 'Recordings')
        self.sidebar_page.set_size_request(270, -1)
        self.paned.set_start_child(self.sidebar_page)

        self.navigation = Adw.NavigationView()
        self.navigation.connect('popped', self._settings_popped)
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_start_title_buttons=False)
        self.content_header = header
        self.settings_button = button('Settings', lambda *_: self.show_settings(), 'emblem-system-symbolic')
        header.pack_end(self.settings_button)
        toolbar.add_top_bar(header)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.error_label = label('', 'error')
        self.error_label.set_selectable(True)
        self.error_label.set_visible(False)
        self.error_label.set_margin_start(20)
        self.error_label.set_margin_end(20)
        body.append(self.error_label)
        self.stack = Gtk.Stack(vexpand=True)
        body.append(self.stack)
        self._build_recorder()
        self._build_detail()
        toolbar.set_content(body)
        self.content_page = Adw.NavigationPage.new(toolbar, APP_NAME)
        self.navigation.add(self.content_page)
        self.shell_content_page = Adw.NavigationPage.new(self.navigation, APP_NAME)
        self.paned.set_end_child(self.shell_content_page)
        self.layout.set_visible_child_name('wide')
        self.split.set_show_content(True)

    def _set_compact(self, compact):
        self.content_header.set_show_start_title_buttons(compact)
        if self.settings_page:
            self.settings_header.set_show_start_title_buttons(compact)
        self.metadata_box.set_orientation(Gtk.Orientation.VERTICAL if compact else Gtk.Orientation.HORIZONTAL)
        self.detail_heading.set_orientation(Gtk.Orientation.VERTICAL if compact else Gtk.Orientation.HORIZONTAL)
        if compact:
            self.rail_position = self.paned.get_position()
            self.paned.set_start_child(None)
            self.paned.set_end_child(None)
            self.split.set_sidebar(self.sidebar_page)
            self.split.set_content(self.shell_content_page)
            self.layout.set_visible_child_name('compact')
        else:
            self.split.set_sidebar(None)
            self.split.set_content(None)
            self.paned.set_start_child(self.sidebar_page)
            self.paned.set_end_child(self.shell_content_page)
            self.paned.set_position(getattr(self, 'rail_position', 360))
            self.layout.set_visible_child_name('wide')

    def _build_recorder(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        for side in ('top', 'bottom', 'start', 'end'):
            getattr(box, f'set_margin_{side}')(20)
        box.append(label('Record a conversation', 'title-1'))
        box.append(label('Local only · Audio and transcripts stay on this computer.', 'dim-label'))
        group = Adw.PreferencesGroup(title='Recording', description='Choose the two audio tracks to save.')
        self.title_row = Adw.EntryRow(title='Meeting title')
        group.add(self.title_row)
        self.microphone_row = Adw.ComboRow(title='Microphone', model=Gtk.StringList.new(['Scanning devices…']))
        self.system_row = Adw.ComboRow(title='System audio output', model=Gtk.StringList.new(['Scanning devices…']))
        group.add(self.microphone_row)
        group.add(self.system_row)
        box.append(group)
        self.devices_message = label('Scanning PipeWire devices…', 'dim-label')
        box.append(self.devices_message)
        self.refresh_devices_button = button('Refresh audio devices', lambda *_: self.refresh_devices())
        self.refresh_devices_button.set_halign(Gtk.Align.START)
        box.append(self.refresh_devices_button)
        box.append(label('The selected output monitor records ALL audio routed to that output, not just the call. Use headphones to reduce microphone echo. Tell participants and obtain any required consent before recording.'))
        self.auto_row = Adw.SwitchRow(title='Transcribe after stopping', subtitle='Use the local model; recording can also be transcribed later.')
        auto_group = Adw.PreferencesGroup()
        auto_group.add(self.auto_row)
        box.append(auto_group)
        controls = Gtk.Box(spacing=12)
        self.record_button = button('Record', lambda *_: self.start_recording())
        self.record_button.add_css_class('destructive-action')
        self.stop_button = button('Stop recording', lambda *_: self.stop_recording())
        self.stop_button.add_css_class('destructive-action')
        self.spinner = Gtk.Spinner()
        self.job_label = label('')
        controls.append(self.record_button)
        controls.append(self.stop_button)
        controls.append(self.spinner)
        controls.append(self.job_label)
        box.append(controls)
        self.inference_message = label('', 'dim-label')
        box.append(self.inference_message)

        clamp = Adw.Clamp(maximum_size=760)
        clamp.set_child(box)
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(clamp)
        self.stack.add_named(scroll, 'recorder')

    def _build_detail(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ('top', 'bottom', 'start', 'end'):
            getattr(box, f'set_margin_{side}')(16)
        heading = Gtk.Box(spacing=8)
        self.detail_heading = heading
        self.meeting_title = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self.meeting_title.add_css_class('title-2')
        heading.append(self.meeting_title)
        actions = Gtk.Box(spacing=6)
        self.transcribe_button = button('Transcribe', lambda *_: self.transcribe_selected())
        self.transcribe_button.add_css_class('suggested-action')
        self.copy_button = button('Copy transcript', lambda *_: self.copy_transcript(), 'edit-copy-symbolic')
        self.folder_button = button('Open recording folder', lambda *_: self.open_folder(), 'folder-open-symbolic')
        self.more_button = Gtk.MenuButton(icon_name='view-more-symbolic', tooltip_text='More recording actions')
        self.more_button.update_property([Gtk.AccessibleProperty.LABEL], ['More recording actions'])
        popover = Gtk.Popover()
        menu = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                       margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        self.archive_button = button('Archive', lambda *_: self._archive_selected())
        self.trash_button = button('Move to Trash…', lambda *_: self._confirm_trash())
        self.trash_button.add_css_class('destructive-action')
        menu.append(self.archive_button)
        menu.append(self.trash_button)
        popover.set_child(menu)
        self.more_button.set_popover(popover)
        for action in (self.transcribe_button, self.copy_button, self.folder_button, self.more_button):
            actions.append(action)
        heading.append(actions)
        box.append(heading)
        self.error_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.error_card.add_css_class('card')
        self.error_card.set_visible(False)
        box.append(self.error_card)
        self.transcript_view = TranscriptPane(self._edit_text, self._undo_edit, self._redo_edit,
                                              self._play_segment, self._stop_playback,
                                              speaker_action=self._speaker_action)
        self.transcript_view.set_vexpand(True)
        self.meeting_metadata = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, hexpand=True)
        self.meeting_metadata.add_css_class('caption')
        self.meeting_metadata.add_css_class('dim-label')
        # Wrap whole metadata/control groups, never the segmented buttons themselves.
        self.metadata_box = Gtk.Box(spacing=8)
        self.metadata_box.append(self.meeting_metadata)
        self.transcript_view.controls.set_halign(Gtk.Align.END)
        self.metadata_box.append(self.transcript_view.controls)

        box.append(self.metadata_box)
        self.transcript_status = label('', 'dim-label')
        self.transcript_status.set_selectable(True)
        box.append(self.transcript_status)
        self.detail_job_label = label('', 'dim-label')
        box.append(self.detail_job_label)
        self.review_toolbar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.review_button = button('Review transcript', lambda *_: self._toggle_review())
        self.review_toolbar.append(self.review_button)
        self.review_status = label('', 'dim-label')
        self.review_toolbar.append(self.review_status)
        self.review_actions = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                                         min_children_per_line=1, max_children_per_line=3,
                                         column_spacing=6, row_spacing=6)
        self.review_again = button('Run review again', lambda *_: self._scan_review())
        self.review_actions.insert(self.review_again, -1)
        self.review_actions.insert(button('History', lambda *_: self._show_review_history()), -1)
        self.review_actions.insert(button('Settings', lambda *_: self.show_settings()), -1)
        self.review_toolbar.append(self.review_actions)
        self.review_actions.set_visible(False)
        box.append(self.review_toolbar)
        box.append(self.transcript_view)
        self.stack.add_named(box, 'detail')

    def _play_segment(self, source, start, end, key):
        meeting = self.selected_meeting
        if not meeting or self._session_busy():
            return
        if self.player.active_key == key:
            self._stop_playback()
            return
        self._stop_playback()
        try:
            self.player.play(meeting.path, source, start, end, key)
        except PlaybackError as error:
            self._record_error(meeting.path, 'playback', str(error))

    def _stop_playback(self):
        if self.review_playback:
            self._log_review_playback('stopped')
        self.player.stop()

    def _playback_changed(self, active_key, error):
        if active_key is None and self.review_playback:
            if error:
                self._log_review_playback('failed')
            else:
                context = self.review_playback
                def completed():
                    if self.review_playback == context:
                        self._log_review_playback('completed')
                    return False
                GLib.idle_add(completed)
        if hasattr(self, 'transcript_view'):
            self.transcript_view.set_playback(active_key)
            self._render_review()
        if error and self.selected_meeting:
            self._record_error(self.selected_meeting.path, 'playback', str(error))
        elif active_key is not None and self.selected_meeting:
            path = self.selected_meeting.path
            if self.record_errors.get(path, ('',))[0] == 'playback':
                self.record_errors.pop(path, None)
                self._show_record_error()

    def _session_protected(self, path):
        snapshot = self.controller.snapshot
        return (path in snapshot.queued_sessions
                or (snapshot.transcription_state == 'transcribing' and path == snapshot.transcription_session)
                or (snapshot.state not in ('idle', 'error') and path == snapshot.session))

    def _session_busy(self):
        return (self.pending_action or self.review_scanning or self.closing
                or (self.selected_meeting is not None and self._session_protected(self.selected_meeting.path)))

    def _lanes_finished(self):
        snapshot = self.controller.snapshot
        return (snapshot.state in ('idle', 'error')
                and snapshot.transcription_state in ('idle', 'error') and not snapshot.queued_sessions)

    def _history_job_subtitles(self):
        snapshot = self.controller.snapshot
        row = self.history_list.get_first_child()
        while row:
            if hasattr(row, 'meeting'):
                path = row.meeting.path
                if snapshot.transcription_state == 'transcribing' and path == snapshot.transcription_session:
                    status = 'Transcribing locally'
                elif path in snapshot.queued_sessions:
                    status = f'Queued #{snapshot.queued_sessions.index(path) + 1}'
                elif snapshot.state not in ('idle', 'error') and path == snapshot.session:
                    status = 'Recording' if snapshot.state == 'recording' else 'Finalizing audio' if snapshot.state == 'stopping' else 'Starting recording'
                else:
                    status = ''
                row.metadata.set_text(f'{row.metadata_text} · {status}' if status else row.metadata_text)
            row = row.get_next_sibling()

    def _review_current(self, path, generation):
        return (not self.closing and self.review_mode and self.selected_meeting is not None
                and self.selected_meeting.path == path and generation == self.review_generation)

    def _leave_review(self):
        self._stop_playback()
        if self.review_cancel:
            self.review_cancel.set()
        self.review_generation += 1
        self.review_mode = False
        self.review_scanning = False
        self.review_store = None
        self.review_error = None
        self.review_button.set_label('Review transcript')
        self.review_actions.set_visible(False)
        self.review_status.set_text('')
        self.transcript_view.set_review([])
        self._show_record_error()

    def _toggle_review(self):
        if self.review_mode:
            self._leave_review()
            self._update_controls()
            return
        if self._session_busy() or not self.selected_meeting or not self.transcript_view.has_speech:
            return
        path = self.selected_meeting.path
        def enter():
            if not self.selected_meeting or self.selected_meeting.path != path:
                return
            self.review_mode = True
            self.review_generation += 1
            generation = self.review_generation
            self.review_button.set_label('Done reviewing')
            self.review_actions.set_visible(True)
            self.review_status.set_text('Opening saved review…')
            state = self.edit_states[path]
            def loaded(store):
                if not self._review_current(path, generation):
                    return
                self.review_store = store
                self._render_review()
                generated_revision = hashlib.sha256(json.dumps(state.generated_payload,
                    ensure_ascii=False, sort_keys=True, allow_nan=False,
                    separators=(',', ':')).encode('utf-8')).hexdigest()
                if not any(run['generatedRevision'] == generated_revision for run in store['runs']):
                    self._scan_review()
            self._run(lambda: load_review(path, state), loaded,
                      lambda error: self._review_failed(path, generation, error))
        self._after_edits(enter)

    def _review_failed(self, path, generation, error):
        if not self._review_current(path, generation):
            return
        self.review_scanning = False
        self.review_error = str(error)
        self._show_record_error()

    def _scan_review(self):
        if self._session_busy() or not self.review_mode or not self.selected_meeting:
            return
        path = self.selected_meeting.path
        def scan():
            if not self.review_mode or self.selected_meeting.path != path or not self._flush_settings():
                return
            self._stop_playback()
            self.review_generation += 1
            generation = self.review_generation
            cancel = threading.Event()
            self.review_cancel = cancel
            self.review_scanning = True
            self.review_error = None
            self._show_record_error()
            config = ReviewConfig(self.preferences.review_endpoint, self.preferences.review_model)
            self._update_controls()
            self.review_status.set_text(f'Reviewing transcript with {config.model}… You can leave review at any time.')
            def done(store):
                if not self._review_current(path, generation):
                    return
                self.review_scanning = False
                self.review_store = store
                self.review_error = None
                self.review_again.set_label('Run review again')
                self._show_record_error()
                self._update_controls()
            self._run(lambda: run_review(path, config, cancel=cancel), done,
                      lambda error: self._review_failed(path, generation, error))
        self._after_edits(scan)

    def _render_review(self):
        if not hasattr(self, 'review_status'):
            return
        state = self.edit_states.get(self.selected_meeting.path) if self.selected_meeting else None
        findings = review_findings(self.review_store, state) if self.review_mode and self.review_store and state else []
        for finding in findings:
            finding['alignment'] = excerpt_interval(finding, state)[3]
            finding['playing_excerpt'] = self.player.active_key == ('review', finding['id'], False)
            finding['playing_context'] = self.player.active_key == ('review', finding['id'], True)
        self.transcript_view.set_review(findings, self._review_decision, self._review_play,
                                        enabled=not self._session_busy())
        self.review_status.set_visible(not (self.review_mode and self.review_error))
        self.review_again.get_parent().set_visible('review' not in self.recovery_actions)
        if self.review_mode and self.review_error:
            self.review_status.set_text('')
        elif self.review_mode and self.review_store and not self.review_scanning:
            resolved = sum(f['resolved'] for f in findings)
            self.review_status.set_text(f'{resolved} of {len(findings)} reviewed · {len(findings) - resolved} unresolved'
                                        if findings else 'No active findings. Run review again to scan this transcript.')

    def _review_decision(self, finding_id, decision):
        if self._session_busy() or not self.review_mode or not self.selected_meeting:
            return
        path, generation = self.selected_meeting.path, self.review_generation
        def decide():
            if not self._review_current(path, generation):
                return
            state = self.edit_states[path]
            self._stop_playback()
            self.pending_action = True
            self._update_controls()
            def done(store):
                self.pending_action = False
                if self._review_current(path, generation):
                    self.review_store = store
                self._update_controls()
            def failed(error):
                self.pending_action = False
                self._review_failed(path, generation, error)
            def write_decision():
                revision = load_review(path, state)['revision']
                return record_decision(path, finding_id, decision, state, expected_store_revision=revision)
            self._queue_review_write(write_decision, done, failed)
        self._after_edits(decide)

    def _review_play(self, finding, whole_clip=False):
        if self._session_busy() or not self.selected_meeting:
            return
        key = ('review', finding['id'], whole_clip)
        if self.player.active_key == key:
            self._stop_playback()
            return
        if self.edit_dirty or self.edit_save_inflight:
            path, generation = self.selected_meeting.path, self.review_generation
            def saved():
                if self._review_current(path, generation):
                    self._review_play(finding, whole_clip)
            self._after_edits(saved)
            return
        self._stop_playback()
        state = self.edit_states[self.selected_meeting.path]
        source, start, end, _alignment = excerpt_interval(finding, state)
        if whole_clip:
            segment = state.generated_payload['segments'][finding['segmentIndex']]
            source, start, end = segment['source'], segment['startTime'], segment['endTime']
        self.review_playback = (self.selected_meeting.path, finding['id'], source, start, end,
                                self.review_generation)
        self._log_review_playback('started', finish=False)
        try:
            self.player.play(self.selected_meeting.path, source, start, end, key)
        except PlaybackError as error:
            self._log_review_playback('failed')
            self._record_error(self.selected_meeting.path, 'playback', str(error))

    def _log_review_playback(self, event, finish=True):
        context = self.review_playback
        if not context:
            return
        if finish:
            self.review_playback = None
        path, finding_id, source, start, end, generation = context
        def record():
            return record_playback(path, finding_id, event, source, start, end)
        def done(store):
            if self._review_current(path, generation):
                if not self.review_store or store['revision'] >= self.review_store['revision']:
                    self.review_store = store
        self._queue_review_write(record, done, lambda error: self._review_failed(path, generation, error))

    def _queue_review_write(self, work, done, failed):
        self.review_writes.append((work, done, failed))
        self._save_review_next()

    def _save_review_next(self):
        if self.review_write_active or not self.review_writes:
            return
        work, done, failed = self.review_writes.pop(0)
        self.review_write_active = True
        def finished(result=None, error=None):
            self.review_write_active = False
            if error:
                self.review_close_pending = False
                failed(error)
                self.show_error(f'Review history could not be saved: {error}')
            else:
                done(result)
            self._save_review_next()
            if self.review_close_pending and not self.review_write_active and not self.review_writes:
                self.review_close_pending = False
                self.close()
        self._run(work, lambda result: finished(result), lambda error: finished(error=error))

    def _show_review_history(self):
        if not self.selected_meeting:
            return
        path, generation = self.selected_meeting.path, self.review_generation
        def opened(store):
            if not self._review_current(path, generation):
                return
            self.review_store = store
            window = Adw.Window(transient_for=self, modal=True, title='Review history',
                                default_width=640, default_height=540)
            toolbar = Adw.ToolbarView()
            toolbar.add_top_bar(Adw.HeaderBar())
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                              margin_top=16, margin_bottom=16, margin_start=16, margin_end=16)
            content.append(label('Playback events record starts, stops and completion only. They do not establish hearing or a verified correction.', 'dim-label'))
            for title, field in [('Review runs', 'runs'), ('Correction decisions', 'decisions'), ('Playback events', 'playback')]:
                disclosure = Gtk.Expander(label=f'{title} ({len(store[field])})')
                text = label(json.dumps(store[field], ensure_ascii=False, indent=2))
                text.set_selectable(True)
                text.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
                text.set_width_chars(1)
                disclosure.set_child(text)
                content.append(disclosure)
            scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
            scroll.set_child(content)
            toolbar.set_content(scroll)
            window.set_content(toolbar)
            window.present()
        self._after_edits(lambda: self._run(lambda: load_review(path), opened,
                          lambda error: self._review_failed(path, generation, error)))

    def _present_edit_state(self, state):
        self.transcript_view.set_transcript(state.payload, render_exports(state.payload)['transcript.txt'])
        self.transcript_view.set_history(state)
        self._render_review()
        if self.speaker_rename_id is None:
            self._render_speakers()

    def _edit_text(self, index, text):
        meeting = self.selected_meeting
        if not meeting or self._session_busy() or meeting.path not in self.edit_states:
            return
        now = time.monotonic()
        new_group = self.last_edit is None or self.last_edit[0] != (meeting.path, index) or now - self.last_edit[1] > 1
        self.last_edit = ((meeting.path, index), now)
        try:
            state = edit_transcript_text(self.edit_states[meeting.path], index, text, new_group=new_group)
        except Exception as error:
            self._record_error(meeting.path, 'save', str(error))
            return
        self._changed_edit_state(meeting.path, state)

    def _changed_edit_state(self, path, state):
        self.edit_states[path] = state
        self.edit_dirty.add(path)
        self.edit_save_failed = False
        self._present_edit_state(state)
        if self.edit_save_source:
            GLib.source_remove(self.edit_save_source)
        self.edit_save_source = GLib.timeout_add(350, self._save_edits)

    def _undo_edit(self):
        self._history_edit(undo_transcript_edit)

    def _redo_edit(self):
        self._history_edit(redo_transcript_edit)

    def _history_edit(self, reducer):
        if not self.selected_meeting or self._session_busy():
            return
        path = self.selected_meeting.path
        state = self.edit_states.get(path)
        if state:
            self.last_edit = None
            self._changed_edit_state(path, reducer(state))

    def _save_edits(self):
        if self.edit_save_source:
            GLib.source_remove(self.edit_save_source)
            self.edit_save_source = 0
        if self.edit_save_inflight or not self.edit_dirty:
            return False
        path = next(iter(self.edit_dirty))
        state = self.edit_states[path]
        self.edit_save_inflight = True
        def finished(error=None):
            self.edit_save_inflight = False
            if error:
                self.edit_save_failed = True
                self.edit_transition = None
                self.refreshing_history = True
                (self.archived_collection if self.collection else self.active_collection).set_active(True)
                if self.selected_meeting:
                    row = self.history_list.get_first_child()
                    while row:
                        if row.meeting.path == self.selected_meeting.path:
                            self.history_list.select_row(row)
                            break
                        row = row.get_next_sibling()
                self.refreshing_history = False
                self._record_error(path, 'save', error)
            else:
                if self.edit_states.get(path) is state:
                    self.edit_dirty.discard(path)
                if self.record_errors.get(path, ('',))[0] == 'save':
                    self.record_errors.pop(path, None)
                    self._show_record_error()
                if self.edit_dirty:
                    self._save_edits()
                elif self.edit_transition:
                    continuation = self.edit_transition
                    self.edit_transition = None
                    continuation()
            self._update_controls()
        self._run(lambda: save_transcript_edit_state(path, state), lambda _: finished(), finished)
        return False

    def _after_edits(self, continuation):
        if self.edit_dirty or self.edit_save_inflight:
            self.edit_transition = continuation
            self.edit_save_failed = False
            self._save_edits()
        else:
            continuation()

    def _collection_changed(self, widget, archived):
        if self.refreshing_history or not widget.get_active() or archived == self.collection:
            return
        def change():
            self._stop_playback()
            self._leave_review()
            self.collection = archived
            self.selected_meeting = None
            self.transcript_view.clear()
            self.error_card.set_visible(False)
            self.stack.set_visible_child_name('recorder')
            self._history_loaded(self.history_generation, self.meetings)
            row = self.history_list.get_row_at_index(0)
            if row:
                self.history_list.select_row(row)
        self._after_edits(change)

    def _archive_selected(self):
        self._stop_playback()
        meeting = self.selected_meeting
        self.more_button.popdown()
        if not meeting or self._session_busy():
            return
        def archive():
            if self._session_busy() or self.selected_meeting is None or self.selected_meeting.path != meeting.path:
                return
            self.pending_action = True
            self._update_controls()
            def done(_):
                self.pending_action = False
                self.refresh_history()
            def failed(error):
                self.pending_action = False
                self._record_error(meeting.path, 'manage', error)
                self._update_controls()
            self._run(lambda: set_archived(meeting.path, not meeting.archived), done, failed)
        self._after_edits(archive)

    def _confirm_trash(self):
        self._stop_playback()
        meeting = self.selected_meeting
        self.more_button.popdown()
        if not meeting or self._session_busy():
            return
        dialog = Adw.AlertDialog(heading=f'Move “{meeting.title}” to Trash?',
                                body=f'This moves the whole recording, audio and all transcript exports out of {"Archived" if meeting.archived else "Active"}. Undo restores the whole recording to its original collection.')
        dialog.add_response('cancel', 'Cancel')
        dialog.add_response('trash', 'Move to Trash')
        dialog.set_response_appearance('trash', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response('cancel')
        dialog.set_close_response('cancel')
        dialog.connect('response', lambda _dialog, response: self._trash_confirmed(meeting) if response == 'trash' else self.more_button.grab_focus())
        dialog.present(self)

    def _trash_confirmed(self, meeting):
        self._stop_playback()
        def trash():
            if self._session_busy() or self.selected_meeting is None or self.selected_meeting.path != meeting.path:
                return
            self.pending_action = True
            self._update_controls()
            def done(entry):
                self.pending_action = False
                self.edit_states.pop(meeting.path, None)
                self.record_errors.pop(meeting.path, None)
                self.refresh_history()
                toast = Adw.Toast.new(f'“{meeting.title}” moved to Trash')
                toast.set_button_label('Undo')
                toast.set_timeout(0)
                toast.connect('button-clicked', lambda *_: self._restore_trash(entry, meeting))
                self.toast_overlay.add_toast(toast)
            def failed(error):
                self.pending_action = False
                self._record_error(meeting.path, 'manage', error)
                self._update_controls()
            self._run(lambda: trash_meeting(meeting.path), done, failed)
            self._set_meeting_voice_active(meeting, False)
        self._after_edits(trash)

    def _restore_trash(self, entry, meeting):
        def restore():
            if self.pending_action or self.closing or self._session_protected(entry.original_path):
                self.show_error('Stop the active job before restoring this recording from Trash.')
                retry = Adw.Toast.new('Recording remains in Trash')
                retry.set_button_label('Retry Undo')
                retry.set_timeout(0)
                retry.connect('button-clicked', lambda *_: self._restore_trash(entry, meeting))
                self.toast_overlay.add_toast(retry)
                return
            self.pending_action = True
            self._update_controls()
            def done(path):
                self.pending_action = False
                self.collection = meeting.archived
                (self.archived_collection if meeting.archived else self.active_collection).set_active(True)
                self.completed_session = path
                self._stop_playback()
                self.selected_meeting = meeting
                self.refresh_history()
            def failed(error):
                self.pending_action = False
                self.show_error(f'Could not restore the recording. It remains in Trash. {error}')
                retry = Adw.Toast.new('Recording remains in Trash')
                retry.set_button_label('Retry Undo')
                retry.set_timeout(0)
                retry.connect('button-clicked', lambda *_: self._restore_trash(entry, meeting))
                self.toast_overlay.add_toast(retry)
                self._update_controls()
            self._run(lambda: restore_trashed_meeting(entry), done, failed)
            self._set_meeting_voice_active(meeting, True)
        self._after_edits(restore)
    def _set_meeting_voice_active(self, meeting, active):
        try:
            manifest = json.loads((meeting.path / 'session.json').read_text())
            self._run(lambda: self.voices.set_meeting_active(manifest['id'], active), lambda _: None,
                      lambda error: self._record_error(meeting.path, 'voice', error))
        except (OSError, ValueError, KeyError) as error:
            self._record_error(meeting.path, 'voice', error)


    def _record_error(self, path, kind, error):
        self.record_errors[path] = (kind, str(error))
        self._show_record_error()

    def _show_record_error(self):
        self.recovery_actions = {}
        while child := self.error_card.get_first_child():
            self.error_card.remove(child)
        meeting = self.selected_meeting
        if not meeting:
            self.error_card.set_visible(False)
            return
        issue = self.record_errors.get(meeting.path)
        if issue is None and self.review_mode and self.review_error:
            issue = ('review', self.review_error)
        if issue is None and (meeting.error or meeting.status in ('failed', 'recording', 'starting')):
            issue = ('capture' if meeting.status != 'recorded' else 'transcribe', meeting.error or 'The recording was interrupted before it finished.')
        self.error_card.set_visible(issue is not None)
        if issue is None:
            return
        kind, details = issue
        titles = {'capture': 'Recording did not finish', 'transcribe': 'Transcription could not finish',
                  'save': 'Corrections could not be saved', 'read': 'Saved transcript could not be opened',
                  'manage': 'Recording could not be moved', 'playback': 'Audio could not be played',
                  'review': 'Review could not finish', 'other': 'Action could not finish'}
        messages = {'capture': 'Any files captured remain in the recording folder. Check your audio devices and start a new recording.',
                    'transcribe': 'Saved audio is unchanged. Any previous transcript and corrections are still saved. Check transcription settings, then retry.',
                    'save': 'Your latest corrections remain open here but are not saved. Retry saving before leaving this recording or starting a job.',
                    'read': 'Saved audio and transcript files have not been changed. Open the folder to inspect the files; repair the reported problem before retrying.',
                    'manage': 'The recording has not been removed. Check the technical details and try again.',
                    'playback': 'Your transcript and corrections are unchanged. Check that the saved audio is available and an audio output is connected, then try Play again.',
                    'review': 'Your saved review progress, transcript and corrections are unchanged. Check review settings, then retry.',
                    'other': 'Your saved recording and transcript are unchanged. Check the technical details before trying again.'}
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                          margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        title = label(titles[kind], 'heading')
        title.add_css_class('error')
        content.append(title)
        content.append(label(messages[kind]))
        actions = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                              column_spacing=8, row_spacing=8, min_children_per_line=1,
                              max_children_per_line=3)
        if kind == 'save':
            self.recovery_actions['save'] = button('Retry saving', lambda *_: self._save_edits())
            actions.insert(self.recovery_actions['save'], -1)
        elif kind == 'review':
            self.recovery_actions['review'] = button('Retry review', lambda *_: self._scan_review())
            actions.insert(self.recovery_actions['review'], -1)
            actions.insert(button('Settings', lambda *_: self.show_settings()), -1)
        elif kind == 'transcribe' and meeting.status == 'recorded':
            self.recovery_actions['transcribe'] = button('Retry transcription', lambda *_: self.transcribe_selected())
            actions.insert(self.recovery_actions['transcribe'], -1)
            actions.insert(button('Settings', lambda *_: self.show_settings()), -1)
        elif kind == 'capture':
            self.recovery_actions['record'] = button('New recording', lambda *_: self.show_recorder())
            actions.insert(self.recovery_actions['record'], -1)
        elif kind == 'read':
            self.recovery_actions['read'] = button('Retry', lambda *_: self._select_meeting(meeting, force=True))
            actions.insert(self.recovery_actions['read'], -1)
        actions.insert(button('Open folder', lambda *_: self.open_folder()), -1)
        content.append(actions)
        disclosure = Gtk.Expander(label='Technical details')
        technical = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        text = label(details)
        text.set_selectable(True)
        technical.append(text)
        technical.append(button('Copy technical details', lambda *_: self.get_clipboard().set(details)))
        technical_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                               max_content_height=240, propagate_natural_height=True)
        technical_scroll.set_child(technical)
        disclosure.set_child(technical_scroll)
        content.append(disclosure)
        self.error_card.append(content)
        self._update_controls()

    def _preferences_loaded(self, preferences):
        if self.preference_revision:
            return
        self.preferences = preferences
        self.saved_preferences = preferences
        self.auto_row.set_active(preferences.auto_transcribe)
        self._populate_devices(prefer_saved=True)
        if self.settings_page and not self.settings_dirty:
            self.settings_initializing = True
            self._set_settings_fields({name: getattr(preferences, name) for name in self.settings_fields})
            self.review_settings_fields[0].set_text(preferences.review_endpoint)
            self.review_settings_fields[1].set_text(preferences.review_model)
            self.settings_initializing = False
        self._check_inference()

    def _inference_candidate(self):
        prefs = replace(self.preferences, microphone=None, system=None, auto_transcribe=False)
        if self.settings_dirty:
            prefs = self._draft_preferences(prefs)
        _validate_preferences(prefs)
        return prefs

    def _can_transcribe(self):
        try:
            return self.inference_ready and self._inference_candidate() == self.inference_preferences
        except (DesktopError, ValueError):
            return False

    def _check_inference(self):
        self.inference_ready = False
        try:
            prefs = self._inference_candidate()
        except (DesktopError, ValueError) as error:
            self.inference_message.set_text(f'Transcription needs setup: {error} Open Settings, or turn off automatic transcription to record only.')
            self.inference_message.set_visible(True)
            self._update_controls()
            return
        self.inference_preferences = prefs
        self._update_controls()
        def checked(error=''):
            try:
                if prefs != self._inference_candidate():
                    return
            except (DesktopError, ValueError):
                return
            self.inference_ready = not error
            self.inference_message.set_text(f'Transcription needs setup: {error} Open Settings, or turn off automatic transcription to record only.' if error else '')
            self.inference_message.set_visible(bool(error))
            self._update_controls()
        self._run(lambda: settings_ready(prefs), lambda _: checked(), checked)

    def refresh_devices(self):
        if self.controller.snapshot.state not in ('idle', 'error') or self.pending_action:
            return
        self.refresh_devices_button.set_sensitive(False)
        self.devices_message.set_text('Scanning PipeWire devices…')
        self._run(list_devices, self._devices_loaded, self._devices_failed)

    def _devices_failed(self, error):
        self.refresh_devices_button.set_sensitive(True)
        self.devices_message.set_text(f'Cannot list audio devices: {error} Check PipeWire is running, then refresh.')
        self.show_error(error)

    def _devices_loaded(self, devices):
        self.devices = devices
        self._populate_devices()
        self.refresh_devices_button.set_sensitive(True)
        missing = []
        if not devices['microphones']:
            missing.append('Connect or enable a microphone')
        if not devices['outputs']:
            missing.append('Connect or enable an audio output')
        self.devices_message.set_text('; '.join(missing) + ', then refresh audio devices.' if missing else 'Select the microphone and the output carrying your call audio.')
        self._update_controls()

    def _populate_devices(self, prefer_saved=False):
        for key, row, requested in (('microphones', self.microphone_row, self.preferences.microphone),
                                     ('outputs', self.system_row, self.preferences.system)):
            has_previous = bool(self.device_names[key])
            previous = self._selected_device(key, row)
            requested = requested if prefer_saved or not has_previous else previous
            entries = self.devices[key]
            names = [None] + [entry['name'] for entry in entries]
            titles = ['System default'] + [entry['description'] + (' (current default)' if entry['default'] else '') for entry in entries]
            if requested is not None and requested not in names:
                names.append(requested)
                titles.append(f'Unavailable: {requested}')
            self.device_names[key] = names
            row.set_model(Gtk.StringList.new(titles))
            row.set_selected(names.index(requested) if requested in names else 0)

    def _selected_device(self, key, row):
        names = self.device_names[key]
        index = row.get_selected()
        return names[index] if index < len(names) else None

    def _form_preferences(self):
        return replace(self.preferences,
                       microphone=self._selected_device('microphones', self.microphone_row),
                       system=self._selected_device('outputs', self.system_row),
                       auto_transcribe=self.auto_row.get_active())

    def refresh_history(self):
        self.history_generation += 1
        generation = self.history_generation
        self._run(list_meetings, lambda meetings: self._history_loaded(generation, meetings))

    def _history_loaded(self, generation, meetings):
        if generation != self.history_generation:
            return
        selected_path = self.selected_meeting.path if self.selected_meeting else None
        completed = self.completed_session
        self.completed_session = None
        self.meetings = meetings
        self.refreshing_history = True
        completed_meeting = next((meeting for meeting in meetings if meeting.path == completed), None)
        if completed_meeting and self.settings_page is None:
            self.collection = completed_meeting.archived
            (self.archived_collection if self.collection else self.active_collection).set_active(True)
        self.history_list.remove_all()
        archived_count = sum(meeting.archived for meeting in meetings)
        self.active_collection.set_label(f'Active {len(meetings) - archived_count}')
        self.archived_collection.set_label(f'Archived {archived_count}')
        visible = [meeting for meeting in meetings if meeting.archived == self.collection]
        self.history_empty.set_text('No archived recordings.' if self.collection else 'No active recordings. Start a recording to save it here.')
        self.history_empty.set_visible(not visible)
        selected_row = None
        for meeting in visible:
            row = Gtk.ListBoxRow()
            row.meeting = meeting
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            for side in ('top', 'bottom'):
                getattr(content, f'set_margin_{side}')(9)
            for side in ('start', 'end'):
                getattr(content, f'set_margin_{side}')(12)
            heading = Gtk.Box(spacing=8)
            title = Gtk.Label(label=meeting.title, xalign=0, hexpand=True,
                              ellipsize=Pango.EllipsizeMode.END)
            title.set_tooltip_text(meeting.title)
            heading.append(title)
            failed = meeting.status == 'failed' or bool(meeting.error)
            status = 'Failed' if failed else 'Transcribed' if meeting.has_transcript else 'Recorded' if meeting.status == 'recorded' else 'Unfinished recording'
            icon = recording_status_icon(failed, meeting.has_transcript)
            icon.set_tooltip_text(status)
            icon.update_property([Gtk.AccessibleProperty.LABEL], [status])
            if failed:
                icon.add_css_class('error')
            heading.append(icon)
            content.append(heading)
            metadata = Gtk.Label(label=f'{meeting.started_at[:16].replace("T", " ")} · {duration(meeting.duration_seconds)}',
                                 xalign=0, ellipsize=Pango.EllipsizeMode.END)
            metadata.add_css_class('caption')
            metadata.add_css_class('dim-label')
            content.append(metadata)
            row.metadata = metadata
            row.metadata_text = metadata.get_text()
            row.set_child(content)
            self.history_list.append(row)
            if meeting.path == selected_path:
                selected_row = row
            if completed == meeting.path and self.settings_page is None:
                selected_row = row
        if selected_row is None and visible and self.selected_meeting is not None:
            selected_row = self.history_list.get_row_at_index(0)
        if selected_row:
            self.history_list.select_row(selected_row)
        self.refreshing_history = False
        if selected_row:
            self._select_meeting(selected_row.meeting,
                                 navigate=selected_row.meeting.path == completed and self.settings_page is None,
                                 force=selected_row.meeting.path == completed or selected_row.meeting.path in self.refresh_transcripts)
        self.refresh_transcripts.difference_update(meeting.path for meeting in meetings)
        self._history_job_subtitles()
        if not visible and self.selected_meeting is not None:
            self._leave_review()
            self.selected_meeting = None
            self.transcript_view.clear()
            self.stack.set_visible_child_name('recorder')
            self.error_card.set_visible(False)
        self._update_controls()

    def _meeting_selected(self, _list, row):
        if row is not None and not self.refreshing_history:
            meeting = row.meeting
            def selected():
                if self.settings_page:
                    self.navigation.pop()
                self._select_meeting(meeting)
            self._after_edits(selected)

    def _select_meeting(self, meeting, navigate=True, force=False):
        previous = self.selected_meeting
        unchanged = (previous is not None and previous.path == meeting.path
                     and previous.has_transcript == meeting.has_transcript
                     and previous.status == meeting.status and previous.error == meeting.error)
        if previous is None or previous.path != meeting.path or force:
            self._close_speakers()
            self.speaker_open_id = None
            self._leave_review()
            self._stop_playback()
        self.selected_meeting = meeting
        self.archive_button.set_label('Restore to Active' if meeting.archived else 'Archive')
        self.error_label.set_visible(False)
        self._show_record_error()
        if navigate:
            self.split.set_show_content(True)
            self.stack.set_visible_child_name('detail')

        self.meeting_title.set_text(meeting.title)
        self.meeting_title.set_tooltip_text(meeting.title)
        metadata = f'{meeting.started_at[:16].replace("T", " ")} · {duration(meeting.duration_seconds)}'
        self.meeting_metadata.set_text(metadata)
        self.meeting_metadata.set_tooltip_text(metadata)
        self.transcribe_button.set_label('Retranscribe' if meeting.has_transcript else 'Transcribe')
        self.transcribe_button.set_tooltip_text('Retranscribe saved audio' if meeting.has_transcript else 'Transcribe saved audio')
        if unchanged and not force:
            self._update_controls()
            return
        if previous is None or previous.path != meeting.path:
            self.transcript_view.clear()
        self.transcript_generation += 1
        generation = self.transcript_generation
        self.transcript_loading = meeting.has_transcript
        if meeting.has_transcript:
            self.transcript_status.set_text('Loading transcript…')
            self.transcript_status.set_visible(True)
            if force and meeting.path not in self.edit_dirty:
                self.edit_states.pop(meeting.path, None)
            def load():
                return self.edit_states.get(meeting.path) or load_transcript_edit_state(meeting.path)
            self._run(load, lambda result: self._transcript_loaded(generation, result),
                      lambda error: self._transcript_failed(generation, error))
        else:
            self.transcript_status.set_text('No transcript yet. Transcribe the saved audio with your local model.' if meeting.status == 'recorded' else 'Recording did not finish. Start a new recording after checking your audio devices.')
            self.transcript_status.set_visible(not self.error_card.get_visible())
        self._update_controls()

    def _transcript_loaded(self, generation, result):
        if generation != self.transcript_generation:
            return
        self.transcript_loading = False
        if self.selected_meeting.path in self.edit_dirty:
            result = self.edit_states[self.selected_meeting.path]
        self.edit_states[self.selected_meeting.path] = result
        self._present_edit_state(result)
        self._queue_voice_match(result)
        has_speech = self.transcript_view.has_speech
        self.transcript_status.set_text('Transcription finished with no speech detected.' if not has_speech else '')
        self.transcript_status.set_visible(not has_speech)
        self.copy_button.set_sensitive(has_speech)
        self._update_controls()

    # ---- voice learning: confirmation-gated enrollment and tiered matching ----

    def _open_speakers(self, index=None):
        meeting = self.selected_meeting
        state = self.edit_states.get(meeting.path) if meeting else None
        if state is None or state.payload.get('schemaVersion') != 2:
            return
        if index is not None:
            self.speaker_open_id = state.payload['segments'][index].get('speakerId')
            self.speaker_rename_id = None
            self.speaker_correction_index = None
        self.speaker_panel_open = True
        self.sidebar_stack.set_visible_child_name('speakers')
        self.sidebar_heading.set_visible_child_name('back')
        self.sidebar_page.set_title('Speakers')
        self.history_refresh.set_visible(False)
        self._render_speakers()
        if self.layout.get_visible_child_name() == 'compact':
            self.split.set_show_content(False)
        if index is not None:
            control = self.speaker_rename_buttons.get(self.speaker_open_id)
            if control:
                control.grab_focus()

    def _close_speakers(self):
        self.speaker_panel_open = False
        self.speaker_rename_id = None
        self.speaker_correction_index = None
        self.sidebar_stack.set_visible_child_name('recordings')
        self.sidebar_title.set_label('Recordings')
        self.sidebar_page.set_title('Recordings')
        self.sidebar_heading.set_visible_child_name('title')
        self.history_refresh.set_visible(True)

    def _begin_speaker_rename(self, speaker_id):
        self.speaker_rename_id = speaker_id
        self.speaker_correction_index = None
        self._render_speakers()
        self.speaker_name_entry.grab_focus()
        self.speaker_name_entry.select_region(0, -1)

    def _cancel_speaker_form(self):
        self.speaker_rename_id = None
        self.speaker_correction_index = None
        self._render_speakers()

    def _apply_speaker_name(self, speaker_id, entry, error_label):
        meeting = self.selected_meeting
        if not meeting or self._session_busy():
            return
        try:
            state = rename_speaker(self.edit_states[meeting.path], speaker_id, entry.get_text())
        except Exception as error:
            error_label.set_label(str(error))
            return
        self.speaker_rename_id = None
        self._changed_edit_state(meeting.path, state)

    def _render_speakers(self):
        if not self.speaker_panel_open:
            return
        meeting = self.selected_meeting
        state = self.edit_states.get(meeting.path) if meeting else None
        if state is None:
            self._close_speakers()
            return
        payload = state.payload
        self.speaker_panel_generation = getattr(self, 'speaker_panel_generation', 0) + 1
        generation = self.speaker_panel_generation
        while child := self.speaker_cards.get_first_child():
            self.speaker_cards.remove(child)
        self.speaker_rename_buttons = {}
        self.speaker_occurrences = {}
        self.speaker_cards.append(label('Speakers', 'title-3'))
        self.speaker_cards.append(label('Names apply to this recording. Me/Them are audio tracks.', 'dim-label'))
        groups = {}
        for index, segment in enumerate(payload['segments']):
            groups.setdefault(segment.get('speakerId'), []).append((index, segment))
        for speaker in payload['speakers']:
            speaker_id = speaker['id']
            turns = groups.get(speaker_id, [])
            name = speaker.get('name') or speaker['label']
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                           margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
            frame = Adw.Bin(child=card, css_classes=['card'])
            caption = name if speaker.get('name') else f'{name} ({speaker["source"].title()})'
            card.append(label(caption, 'heading'))
            confirmed = sum(segment.get('speakerConfirmed') == speaker_id for _, segment in turns)
            card.append(label(f'{len(turns)} snippets · {confirmed} confirmed', 'dim-label'))
            actions = Gtk.Box(spacing=6)
            rename = button('Rename all', lambda *_, sid=speaker_id: self._begin_speaker_rename(sid))
            rename.set_sensitive(not self._session_busy())
            actions.append(rename)
            self.speaker_rename_buttons[speaker_id] = rename
            profile = self.voices.profile_for_name(name, speaker['source'])
            if profile:
                forget = button('Forget voice', lambda *_, pid=profile.id: self._forget_voice(pid))
                actions.append(forget)
                card.append(label(f'Remembered · {len(profile.samples)} voice samples', 'dim-label'))
            card.append(actions)
            if self.speaker_rename_id == speaker_id:
                card.append(label(f'New name for all {len(turns)} snippets'))
                entry = Gtk.Entry(text=name, max_length=60, hexpand=True)
                self.speaker_name_entry = entry
                card.append(entry)
                error = label('', 'error')
                card.append(error)
                row = Gtk.Box(spacing=6)
                row.append(button('Apply', lambda *_, sid=speaker_id, e=entry, err=error: self._apply_speaker_name(sid, e, err)))
                row.append(button('Cancel', lambda *_: self._cancel_speaker_form()))
                card.append(row)
                card.append(label('Audio, timing and playback stay unchanged.', 'dim-label'))
            hint = self.voice_hints.get(speaker_id)
            if hint:
                card.append(label(f'Voice match: {hint["name"]} ({hint["score"]:.2f})', 'dim-label'))
            occurrences = Gtk.Expander(label=f'{len(turns)} snippets')
            listing = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16, margin_top=8)
            occurrences.set_child(listing)
            self.speaker_occurrences[speaker_id] = occurrences
            def expanded(widget, _param=None, rows=turns, column=listing):
                if not widget.get_expanded() or column.get_first_child():
                    return
                remaining = iter(rows)
                def pump():
                    if generation != self.speaker_panel_generation or not self.speaker_panel_open:
                        return GLib.SOURCE_REMOVE
                    for _ in range(12):
                        item = next(remaining, None)
                        if item is None:
                            return GLib.SOURCE_REMOVE
                        self._append_speaker_occurrence(column, payload, *item)
                    return GLib.SOURCE_CONTINUE
                if pump() == GLib.SOURCE_CONTINUE:
                    GLib.idle_add(pump)
            occurrences.connect('notify::expanded', expanded)
            card.append(occurrences)
            self.speaker_cards.append(frame)
            occurrences.set_expanded(speaker_id == self.speaker_open_id)

    def _append_speaker_occurrence(self, column, payload, index, segment):
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        row.append(label(f'{duration(segment["startTime"])}–{duration(segment["endTime"])}', 'dim-label'))
        excerpt = label(' '.join(segment['text'].split()))
        excerpt.set_lines(3)
        excerpt.set_ellipsize(Pango.EllipsizeMode.END)
        row.append(excerpt)
        feedback = ('Confirmed' if segment.get('speakerConfirmed') else
                    'Marked Wrong · needs correction' if segment.get('speakerRejected') else 'Not reviewed')
        row.append(label(feedback, 'dim-label'))
        actions = Gtk.Box(spacing=6)
        for caption, kind in (('✓ Right', 'confirm'), ('✕ Wrong', 'wrong'), ('Show', 'show')):
            control = button(caption, lambda *_, action=kind, i=index: self._speaker_action(action, i, None))
            control.set_sensitive(kind == 'show' or not self._session_busy())
            actions.append(control)
        row.append(actions)
        if self.speaker_correction_index == index:
            row.append(label('Who said this?', 'heading'))
            row.append(label(segment['text']))
            for speaker in payload['speakers']:
                if speaker['source'] == segment['source'] and speaker['id'] != segment.get('speakerId'):
                    row.append(button(speaker.get('name') or speaker['label'],
                                      lambda *_, sid=speaker['id'], i=index: self._speaker_action('reassign', i, sid)))
            new_person = Gtk.Expander(label='New person')
            form = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            entry = Gtk.Entry(placeholder_text='Name', max_length=60)
            form.append(entry)
            error = label('', 'error')
            form.append(error)
            def create(*_):
                if not entry.get_text().strip():
                    error.set_label('Enter a name.')
                    return
                self._speaker_action('create', index, entry.get_text())
            form.append(button('Create person and apply', create))
            new_person.set_child(form)
            row.append(new_person)
            row.append(button('Cancel', lambda *_: self._cancel_speaker_form()))
        column.append(row)

    def _forget_voice(self, profile_id):
        meeting = self.selected_meeting
        self._run(lambda: self.voices.forget(profile_id), lambda _: self._render_speakers(),
                  lambda error: self._record_error(meeting.path, 'voice', error))

    def _speaker_action(self, kind, index, value):
        meeting = self.selected_meeting
        if not meeting or meeting.path not in self.edit_states:
            return
        if kind == 'open':
            self._open_speakers(index)
            return
        if kind == 'show':
            self.split.set_show_content(True)
            self.transcript_view.show_segment(index)
            return
        if self._session_busy():
            return
        state = self.edit_states[meeting.path]
        before = state.payload['segments'][index]
        newly_confirmed = kind == 'confirm' and before.get('speakerConfirmed') != before.get('speakerId')
        try:
            if kind == 'confirm':
                state = confirm_segment(state, index)
                self.speaker_correction_index = None
            elif kind == 'wrong':
                state = reject_segment(state, index)
                hint = self.voice_hints.get(before.get('speakerId'))
                if hint and hint['tier'] == 'auto' and not before.get('speakerRejected'):
                    self._run(lambda: self.voices.record_wrong_auto(hint['profileId']), lambda _: None,
                              lambda error: self._record_error(meeting.path, 'voice', error))
                self.speaker_rename_id = None
                self.speaker_correction_index = index
            elif kind == 'reassign':
                state = reassign_segment(state, index, value)
                self.speaker_correction_index = None
            elif kind == 'create':
                state = create_speaker_and_reassign(state, index, value)
                self.speaker_correction_index = None
            else:
                return
        except Exception as error:
            self._record_error(meeting.path, 'save', error)
            return
        self.speaker_open_id = state.payload['segments'][index].get('speakerId')
        if state != self.edit_states[meeting.path]:
            self._changed_edit_state(meeting.path, state)
        else:
            self._render_speakers()
        if newly_confirmed:
            self._maybe_offer_enrollment(state.payload, index)


    def _maybe_offer_enrollment(self, payload, index):
        if self.voice_offer_dialog is not None:
            return
        segment = payload['segments'][index]
        speaker = next((entry for entry in payload['speakers']
                        if entry['id'] == segment.get('speakerId')), None)
        if not speaker or not segment.get('speakerConfirmed'):
            return
        name = speaker.get('name') or speaker['label']
        if self.voices.profile_for_name(name, speaker['source']):
            return
        meeting = self.selected_meeting
        key = (meeting.path, payload['generation']['id'], speaker['id'])
        confirmed = sum(s.get('speakerConfirmed') == speaker['id'] for s in payload['segments'])
        if confirmed <= self.voice_offer_counts.get(key, 0):
            return
        self.voice_offer_counts[key] = confirmed
        dialog = Adw.AlertDialog(heading=f'Remember {name}’s voice?',
                                 body='Only snippets you confirm with ✓ Right. Stays on this computer; forget anytime.')
        self.voice_offer_dialog = dialog
        dialog.add_response('not-now', 'Not now')
        dialog.add_response('remember', 'Remember voice')
        dialog.set_response_appearance('remember', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('remember')
        dialog.set_close_response('not-now')
        def responded(_dialog, response):
            self.voice_offer_dialog = None
            if response == 'remember':
                current = self.edit_states.get(meeting.path)
                if current and current.payload['generation']['id'] == payload['generation']['id']:
                    self._enroll_speaker(meeting, current.payload, speaker['id'], name, speaker['source'])
        dialog.connect('response', responded)
        dialog.present(self)

    def _enroll_speaker(self, meeting, payload, speaker_id, name, source):
        indices = [index for index, segment in enumerate(payload['segments'])
                   if segment.get('speakerConfirmed') == speaker_id]
        def enroll():
            manifest = json.loads((meeting.path / 'session.json').read_text())
            profile = self.voices.profile_for_name(name, source)
            enrolled = {sample['segmentIndex'] for sample in profile.samples
                        if sample['meetingId'] == manifest['id']} if profile else set()
            for index in indices:
                if index not in enrolled:
                    windows = windows_for_segment(payload['segments'][index], manifest['durationSeconds'])
                    vectors = embed_windows(meeting.path, windows)
                    self.voices.enroll(name, source, manifest['id'], index, vectors)
        def done(_):
            if self.selected_meeting and self.selected_meeting.path == meeting.path:
                self._queue_voice_match(self.edit_states[meeting.path])
                self._render_speakers()
        self._run(enroll, done, lambda error: self._record_error(meeting.path, 'voice', error))


    def _queue_voice_match(self, state):
        meeting = self.selected_meeting
        if not meeting or not self.voices.master or not self.voices.profiles:
            self.voice_hints = {}
            self.transcript_view.set_voice_hints({})
            return
        payload = state.payload
        if payload.get('schemaVersion') != 2:
            return
        self.voice_generation += 1
        generation = self.voice_generation
        manifest = json.loads((meeting.path / 'session.json').read_text())
        def match():
            hints = {}
            by_speaker = {}
            for index, segment in enumerate(payload['segments']):
                if not (segment.get('speakerId') and segment['text'].strip() and not segment.get('speakerConfirmed')):
                    continue
                speaker_id = segment['speakerId']
                if speaker_id not in by_speaker and windows_for_segment(segment, manifest['durationSeconds']):
                    by_speaker[speaker_id] = index
            windows, keys = [], []
            for speaker_id, index in by_speaker.items():
                for window in windows_for_segment(payload['segments'][index], manifest['durationSeconds'])[:1]:
                    windows.append(window)
                    keys.append(speaker_id)
            if not windows:
                return {}
            vectors = embed_windows(meeting.path, windows)
            for speaker_id, vector in zip(keys, vectors):
                profile, score, tier = self.voices.match(vector)
                if profile and tier:
                    hints[speaker_id] = {'name': profile.name, 'score': round(score, 3),
                                         'tier': tier, 'profileId': profile.id}
            return hints
        def done(hints):
            if generation != self.voice_generation or not self.selected_meeting:
                return
            self.voice_hints = hints
            self.transcript_view.set_voice_hints(hints)
            self._present_edit_state(self.edit_states[meeting.path])
        self._run(match, done, lambda error: self._record_error(meeting.path, 'voice', error))

    def _voice_settings_group(self):
        group = Adw.PreferencesGroup(title='Remembered voices',
            description='Local-only voice learning. Enrollment happens only from snippets you confirm with ✓; auto-matching needs the master switch. Correction counts are recorded but nothing retrains automatically.')
        master = Adw.SwitchRow(title='Match remembered voices',
                               subtitle=(f'{len(self.voices.profiles)} remembered · auto-match at similarity ≥ '
                                         f"{dict(strict='0.65', balanced='0.60', relaxed='0.50')[self.voices.strictness]}"
                                         if self.voices.master else 'Off · nothing is remembered'))
        master.set_active(self.voices.master)
        master.connect('notify::active', lambda *_: self._run(
            lambda: self.voices.set_master(master.get_active()),
            lambda _: self._refresh_voice_settings(), lambda error: self.show_error(str(error))))
        group.add(master)
        strictness = Adw.ComboRow(title='Auto-match strictness', model=Gtk.StringList.new(['Strict', 'Balanced', 'Relaxed']))
        strictness.set_selected(('strict', 'balanced', 'relaxed').index(self.voices.strictness))
        strictness.connect('notify::selected', lambda *_: self._run(
            lambda: self.voices.set_strictness(('strict', 'balanced', 'relaxed')[strictness.get_selected()]),
            lambda _: None, lambda error: self.show_error(str(error))))
        group.add(strictness)
        for profile in self.voices.profiles:
            samples = sum(1 for sample in profile.samples if sample.get('active', True))
            row = Adw.ActionRow(title=profile.name,
                                subtitle=f'{samples} sample{"s" if samples != 1 else ""} · {profile.wrong_auto} auto-match correction{"s" if profile.wrong_auto != 1 else ""} recorded')
            forget = Gtk.Button(label='Forget')
            forget.add_css_class('destructive-action')
            forget.connect('clicked', lambda *_, pid=profile.id: self._run(
                lambda: self.voices.forget(pid),
                lambda _: self._refresh_voice_settings(), lambda error: self.show_error(str(error))))
            row.add_suffix(forget)
            group.add(row)
        return group

    def _refresh_voice_settings(self):
        if getattr(self, 'voice_group_slot', None):
            self.voice_group_slot.set_child(self._voice_settings_group())
        if self.selected_meeting and self.selected_meeting.path in self.edit_states:
            self._queue_voice_match(self.edit_states[self.selected_meeting.path])

    def _transcript_failed(self, generation, error):
        if generation == self.transcript_generation:
            self.transcript_loading = False
            self.transcript_view.clear()
            self.transcript_status.set_text(f'Cannot read transcript: {error}')
            self.transcript_status.set_visible(True)
            self.copy_button.set_sensitive(False)
            self._record_error(self.selected_meeting.path, 'read', error)

    def show_recorder(self):
        self._stop_playback()
        self._after_edits(self._show_recorder)

    def _show_recorder(self):
        if self.settings_page:
            self.navigation.pop()
        self._close_speakers()
        self.speaker_open_id = None
        self._leave_review()
        self.selected_meeting = None
        self.error_card.set_visible(False)
        self.error_label.set_visible(False)
        self.transcript_generation += 1
        self.history_list.unselect_all()
        self.stack.set_visible_child_name('recorder')
        self.split.set_show_content(True)
        self.title_row.grab_focus()
        self._update_controls()

    def show_error(self, error):
        if error and self.selected_meeting and self.settings_page is None:
            self._record_error(self.selected_meeting.path, 'other', error)
        else:
            self.error_label.set_text(str(error))
            self.error_label.set_visible(bool(error) and self.selected_meeting is None)
        if self.settings_page:
            self.settings_message.set_text(str(error))
        return False

    def _begin(self, work, *, kind='capture', session=None):
        if self.pending_action or self.closing or (kind == 'capture' and self.controller.snapshot.state not in ('idle', 'error')):
            return

        self.show_error('')
        self.pending_action = True
        self._update_controls()
        def done(_):
            self.pending_action = False
            self._apply_snapshot(self.controller.snapshot)
        def failed(error):
            self.pending_action = False
            if session:
                self._record_error(session, kind, error)
            else:
                self.show_error(error)
            self._update_controls()
        self._run(work, done, failed)

    def start_recording(self):
        self._stop_playback()
        self._after_edits(self._start_recording)

    def _start_recording(self):
        if not self._flush_settings():
            return
        prefs = self._form_preferences()
        title = self.title_row.get_text()
        self._queue_preferences(prefs)
        self._begin(lambda: self.controller.start_recording(title, prefs))

    def stop_recording(self):
        self._run(self.controller.stop_recording, failed=self.show_error)

    def transcribe_selected(self):
        self._stop_playback()
        meeting = self.selected_meeting
        if not meeting or meeting.status != 'recorded' or self._session_busy() or self.review_write_active or self.review_writes or not self._can_transcribe():
            return
        state = self.edit_states.get(meeting.path)
        if state and state.has_corrections:
            dialog = Adw.AlertDialog(heading='Replace your transcript corrections?',
                                    body='Retranscription replaces the corrected text only after it succeeds. You can Undo that replacement. Failed or cancelled transcription keeps your corrections.')
            dialog.add_response('cancel', 'Cancel')
            dialog.add_response('replace', 'Retranscribe')
            dialog.set_default_response('cancel')
            dialog.set_close_response('cancel')
            dialog.connect('response', lambda _dialog, response: self._transcribe_meeting(meeting) if response == 'replace' else None)
            dialog.present(self)
        else:
            self._transcribe_meeting(meeting)

    def _transcribe_meeting(self, meeting):
        def begin():
            if self._session_busy() or self.review_write_active or self.review_writes or not self._can_transcribe() or not self.selected_meeting or self.selected_meeting.path != meeting.path or meeting.status != 'recorded':
                return
            if not self._flush_settings():
                return
            self._leave_review()
            self.record_errors.pop(meeting.path, None)
            self._show_record_error()
            self._begin(lambda: self.controller.transcribe(meeting.path, self._form_preferences()),
                        kind='transcribe', session=meeting.path)
        self._after_edits(begin)

    def _controller_changed(self, snapshot):
        GLib.idle_add(self._apply_snapshot, snapshot)

    def _apply_snapshot(self, snapshot):
        previous = self.last_snapshot
        self.last_snapshot = snapshot
        capture_finished = (previous is not None and previous.state not in ('idle', 'error')
                            and snapshot.state in ('idle', 'error'))
        transcription_finished = (previous is not None and previous.transcription_state == 'transcribing'
                                  and (snapshot.transcription_state != 'transcribing'
                                       or snapshot.transcription_session != previous.transcription_session))
        if capture_finished:
            self.completed_session = snapshot.session if self.selected_meeting is None else None
        if transcription_finished:
            self.refresh_transcripts.add(previous.transcription_session)
            if previous.transcription_session not in self.edit_dirty:
                self.edit_states.pop(previous.transcription_session, None)
        if snapshot.error:
            if snapshot.session:
                self._record_error(snapshot.session, 'capture', snapshot.error)
            elif not self.selected_meeting:
                self.show_error(snapshot.error)
        if snapshot.transcription_error and snapshot.transcription_session:
            self._record_error(snapshot.transcription_session, 'transcribe', snapshot.transcription_error)
        capture_started = (snapshot.state == 'starting' and (previous is None or previous.state != 'starting'))
        if capture_started and self.settings_page is None:
            self.stack.set_visible_child_name('recorder')
            self.split.set_show_content(True)
        self._update_controls()
        self._history_job_subtitles()
        if capture_finished or transcription_finished or (previous is None):
            self.refresh_history()
        if (self.closing and self.shutdown_complete and self._lanes_finished()
                and not self.save_inflight and self.save_pending is None):
            self.get_application().quit()
        self._tick()
        return False

    def _update_controls(self):
        snapshot = self.controller.snapshot
        state = snapshot.state
        busy = self._session_busy()
        capture_busy = self.pending_action or state not in ('idle', 'error') or self.closing
        self.record_button.set_sensitive(not capture_busy and bool(self.devices['microphones']) and bool(self.devices['outputs']))
        self.record_button.set_visible(state not in ('starting', 'recording', 'stopping'))
        self.stop_button.set_visible(state in ('starting', 'recording', 'stopping'))
        self.stop_button.set_sensitive(state in ('starting', 'recording') and not self.closing)
        for widget in (self.title_row, self.microphone_row, self.system_row, self.auto_row, self.refresh_devices_button):
            widget.set_sensitive(not capture_busy)
        inference_allowed = not busy and not self.review_write_active and not self.review_writes and self._can_transcribe() and self.selected_meeting is not None and self.selected_meeting.status == 'recorded'
        self.transcribe_button.set_sensitive(inference_allowed)
        for kind, widget in self.recovery_actions.items():
            allowed = inference_allowed if kind == 'transcribe' else not busy
            if kind == 'save':
                allowed = allowed and not self.edit_save_inflight
            elif kind == 'read':
                allowed = allowed and not self.edit_save_inflight and not self.transcript_loading
            widget.set_sensitive(allowed)
        self.more_button.set_sensitive(not busy and self.selected_meeting is not None)
        self.archive_button.set_sensitive(not busy)
        self.trash_button.set_sensitive(not busy)
        self.active_collection.set_sensitive(not self.closing)
        self.archived_collection.set_sensitive(not self.closing)
        self.transcript_view.set_history(self.edit_states.get(self.selected_meeting.path) if self.selected_meeting else None)
        self.transcript_view.set_editable(not busy and not self.transcript_loading)
        self.transcript_view.set_playback_enabled(not busy and not self.transcript_loading)
        self.transcribe_button.set_visible(self.selected_meeting is not None and self.selected_meeting.status == 'recorded'
                                           and 'transcribe' not in self.recovery_actions)
        self.copy_button.set_sensitive(self.transcript_view.has_speech)
        self.folder_button.set_sensitive(self.selected_meeting is not None)
        self.history_list.set_sensitive(not self.closing)
        self.review_button.set_sensitive(self.review_mode or (not busy and self.transcript_view.has_speech))
        self.review_again.set_sensitive(not busy)
        self._render_review()
        if self.settings_page:
            self.settings_stop_button.set_visible(state in ('starting', 'recording', 'stopping'))
            self.settings_stop_button.set_sensitive(state in ('starting', 'recording') and not self.closing)

            for widget in (*self.settings_fields.values(), *self.review_settings_fields):
                widget.set_sensitive(not self.closing)
        active = not self._lanes_finished() or self.pending_action or self.closing
        if active:
            self.spinner.start()
        else:
            self.spinner.stop()
        self.spinner.set_visible(active)

    def _tick(self):
        if self.closing:
            self.job_label.set_text('Finalizing audio / cancelling transcription before closing…')
        else:
            snapshot = self.controller.snapshot
            state = snapshot.state
            if state == 'recording' and snapshot.started_at is not None:
                capture = f'Recording · {duration(time.monotonic() - snapshot.started_at)}'
            else:
                capture = {'starting': 'Starting recording…', 'stopping': 'Finalizing audio…'}.get(state, '')
            jobs = []
            if capture:
                jobs.append(capture)
            if snapshot.transcription_state == 'transcribing':
                meeting = next((item for item in self.meetings if item.path == snapshot.transcription_session), None)
                jobs.append(f'Transcribing locally: {meeting.title if meeting else snapshot.transcription_session.name}')
            if snapshot.queued_sessions:
                titles = {item.path: item.title for item in self.meetings}
                jobs.append('Waiting: ' + '; '.join(f'{index}. {titles.get(path, path.name)}'
                                                  for index, path in enumerate(snapshot.queued_sessions, 1)))
            self.job_label.set_text(' · '.join(jobs))
        text = self.job_label.get_text()
        active = self.closing or self.pending_action or not self._lanes_finished()
        self.job_label.set_visible(active)
        self.detail_job_label.set_text(text if active else '')
        self.detail_job_label.set_visible(active)
        if self.settings_page:
            self.settings_job_label.set_text(text if active else '')
            self.settings_job_label.set_visible(active)
        return True

    def copy_transcript(self):
        self.get_clipboard().set(self.transcript_view.text)

    def open_folder(self):
        if self.selected_meeting:
            launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(self.selected_meeting.path)))
            def opened(source, result):
                try:
                    source.launch_finish(result)
                except GLib.Error as error:
                    self.show_error(f'Cannot open recording folder: {error.message}')
            launcher.launch(self, None, opened)

    def _set_settings_fields(self, values):
        for name, row in self.settings_fields.items():
            value = values[name]
            if name == 'profile':
                row.set_selected(PROFILE_CHOICES.index(value))
            elif name == 'threads':
                row.set_value(value)
            elif name == 'use_gpu':
                row.set_active(value)
            else:
                row.set_text(str(value))
        self._show_profile_paths()

    def _show_profile_paths(self):
        profile = PROFILE_CHOICES[self.settings_fields['profile'].get_selected()]
        prefix = 'parakeet' if profile == 'multitalker-parakeet' else 'nemotron' if profile else None
        if profile:
            defaults = PROFILE_DEFAULTS[profile]
            self.settings_fields['profile'].set_subtitle(
                f'Up to {defaults.capacity} distinct speakers per mixed source' +
                (' · English only' if prefix == 'parakeet' else ' · 32 language locales'))
        else:
            self.settings_fields['profile'].set_subtitle('Select a model before transcription; saved Whisper settings need an explicit choice.')
        for name in PREFERENCE_PATHS:
            self.settings_fields[name].set_visible(prefix is not None and name.startswith(prefix))

    def _draft_preferences(self, prefs):
        values = dict(self.settings_draft)
        for name in PREFERENCE_PATHS:
            if not values[name].strip():
                raise ValueError('Enter absolute local model and runtime paths.')
            values[name] = Path(values[name].strip()).expanduser()
        values['language'] = values['language'].strip()
        values['threads'] = int(values['threads'])
        return replace(prefs, **values)

    def _settings_changed(self, *_):
        if self.settings_initializing:
            return
        self.settings_dirty = True
        self.settings_draft = {}
        for name, row in self.settings_fields.items():
            self.settings_draft[name] = (PROFILE_CHOICES[row.get_selected()] if name == 'profile' else
                row.get_value() if name == 'threads' else row.get_active() if name == 'use_gpu' else row.get_text())
        self._show_profile_paths()
        self.review_settings_draft = tuple(row.get_text() for row in self.review_settings_fields)
        if self.autosave_source:
            GLib.source_remove(self.autosave_source)
        self.autosave_source = GLib.timeout_add(350, self._autosave_settings)
        self._check_inference()

    def _autosave_settings(self):
        self.autosave_source = 0
        self._flush_settings()
        return False

    def _flush_settings(self):
        if self.autosave_source:
            GLib.source_remove(self.autosave_source)
            self.autosave_source = 0
        if not self.settings_dirty:
            return True
        try:
            prefs = replace(self._draft_preferences(self._form_preferences()),
                            review_endpoint=self.review_settings_draft[0].strip(),
                            review_model=self.review_settings_draft[1].strip())
            _validate_preferences(prefs)
        except (DesktopError, ValueError) as error:
            self.show_error(str(error))
            return False
        self.settings_dirty = False
        self.show_error('')
        self._queue_preferences(prefs)
        return True

    def _queue_preferences(self, prefs):
        _validate_preferences(prefs)
        if prefs != self.preferences or prefs != self.saved_preferences:
            self.preference_revision += 1
        self.preferences = prefs
        if self.save_inflight or prefs != self.saved_preferences:
            self.save_pending = prefs
            self._save_next()

    def _save_next(self):
        if self.save_inflight or self.save_pending is None:
            return
        prefs = self.save_pending
        self.save_pending = None
        self.save_inflight = True
        def finished(error=None):
            self.save_inflight = False
            if error:
                self.close_after_save = False
                self.closing = False
                self.show_error(f'Cannot save settings: {error}')
                self._update_controls()
            else:
                self.saved_preferences = prefs
                if prefs == self.preferences:
                    self._check_inference()
            if self.save_pending is not None:
                self._save_next()
            elif not error and (self.close_after_save or (self.closing and self.shutdown_complete)) and self._lanes_finished():
                if self._flush_settings() and not self.save_inflight and self.save_pending is None:
                    self.get_application().quit()
        self._run(lambda: save_preferences(prefs), lambda _: finished(), finished)

    def show_settings(self):
        self._stop_playback()
        self._after_edits(self._show_settings)

    def _show_settings(self):
        self.split.set_show_content(True)
        if self.settings_page:
            return
        toolbar = Adw.ToolbarView()
        settings_header = Adw.HeaderBar(show_start_title_buttons=self.layout.get_visible_child_name() == 'compact')
        self.settings_header = settings_header
        toolbar.add_top_bar(settings_header)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        for side in ('top', 'bottom', 'start', 'end'):
            getattr(body, f'set_margin_{side}')(20)
        group = Adw.PreferencesGroup(title='Transcription model',
            description='Choose manually before transcription. Limits apply to distinct voices in each mixed audio source; neither model supports more than 8. No automatic switching or fallback.')
        body.append(group)
        profile = Adw.ComboRow(title='Model', model=Gtk.StringList.new(
            ['Choose a transcription model'] + [PROFILE_DEFAULTS[key].label.split(' (')[0] for key in PROFILE_CHOICES[1:]]))
        paths = {
            'parakeet_model': 'Parakeet Q8 bundle path',
            'parakeet_runtime': 'Parakeet native executable path',
            'nemotron_model': 'Nemotron 3.5 ASR checkpoint path',
            'nemotron_runtime': 'Nemotron isolated Python path',
            'nemotron_diarizer': 'Nemotron diarization checkpoint path',
        }
        self.settings_fields = {'profile': profile}
        for name, title in paths.items():
            self.settings_fields[name] = Adw.EntryRow(title=title)
        language = Adw.EntryRow(title='Language locale (en-US, en-GB; Parakeet is English-only)')
        threads = Adw.SpinRow.new_with_range(1, 256, 1)
        threads.set_title('CPU threads')
        gpu = Adw.SwitchRow(title='Use GPU', subtitle='Requires the selected runtime’s GPU backend. Turn off to choose CPU explicitly.')
        self.settings_fields.update(language=language, threads=threads, use_gpu=gpu)
        self._set_settings_fields({name: getattr(self.preferences, name) for name in self.settings_fields})
        for row in self.settings_fields.values():
            group.add(row)
        review_group = Adw.PreferencesGroup(title='Transcript review',
            description='Text-only review with an explicitly configured local model. No automatic cloud fallback. Audio is not sent.')
        endpoint = Adw.EntryRow(title='Local OpenAI-compatible endpoint', text=self.preferences.review_endpoint)
        review_model = Adw.EntryRow(title='Review model', text=self.preferences.review_model)
        self.review_settings_fields = (endpoint, review_model)
        for row in self.review_settings_fields:
            review_group.add(row)
            row.connect('changed', self._settings_changed)
        body.append(review_group)
        self.voice_group_slot = Adw.Bin()
        self.voice_group_slot.set_child(self._voice_settings_group())
        body.append(self.voice_group_slot)
        message = label(self.error_label.get_text(), 'dim-label')
        message.set_selectable(True)
        self.settings_message = message
        body.append(message)

        job = Gtk.Box(spacing=12)
        self.settings_job_label = label(self.job_label.get_text())
        self.settings_job_label.set_hexpand(True)
        self.settings_stop_button = button('Stop recording', lambda *_: self.stop_recording())
        self.settings_stop_button.add_css_class('destructive-action')
        job.append(self.settings_job_label)
        job.append(self.settings_stop_button)
        body.append(job)
        clamp = Adw.Clamp(maximum_size=760)
        clamp.set_child(body)
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(clamp)
        toolbar.set_content(scroll)
        page = Adw.NavigationPage.new(toolbar, 'Settings')
        self.settings_page = page
        for name, row in self.settings_fields.items():
            row.connect('notify::selected' if name == 'profile' else 'notify::value' if name == 'threads' else
                        'notify::active' if name == 'use_gpu' else 'changed', self._settings_changed)
        self.settings_dirty = False
        if hasattr(self, 'settings_draft'):
            self.settings_initializing = True
            self._set_settings_fields(self.settings_draft)
            if hasattr(self, 'review_settings_draft'):
                endpoint.set_text(self.review_settings_draft[0])
                review_model.set_text(self.review_settings_draft[1])
            self.settings_initializing = False
            self.settings_dirty = True
        self._tick()
        self.navigation.push(page)
        self._update_controls()
        settings_header.child_focus(Gtk.DirectionType.TAB_FORWARD)

    def _settings_popped(self, _navigation, page):
        if page == self.settings_page:
            self._flush_settings()
            self.settings_page = None
            self.settings_button.grab_focus()

    def _close_requested(self, *_):
        self._leave_review()
        self._stop_playback()
        if self.review_write_active or self.review_writes:
            self.review_close_pending = True
            return True
        if self.edit_dirty or self.edit_save_inflight:
            self._after_edits(self.close)
            return True
        if self.closing:
            return True
        if self._lanes_finished() and not self.pending_action:
            if not self._flush_settings():
                return True
            if self.preference_revision and self.preferences != self.saved_preferences:
                self._queue_preferences(self.preferences)
            if self.save_inflight or self.save_pending:
                self.close_after_save = True
                return True
            return False
        dialog = Adw.AlertDialog(heading=f'Close {APP_NAME} with an active job?',
                                 body='An active recording will be stopped and its audio saved. Transcription will be cancelled; saved audio can be transcribed later.')
        dialog.add_response('cancel', 'Keep open')
        dialog.add_response('close', 'Stop job and close')
        dialog.set_response_appearance('close', Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response('cancel')
        dialog.set_close_response('cancel')
        dialog.connect('response', self._close_response)
        dialog.present(self)
        return True

    def _close_response(self, _dialog, response):
        if response != 'close':
            return
        self.shutdown()

    def shutdown(self):
        self._stop_playback()
        self._after_edits(self._shutdown)

    def _shutdown(self):
        if self.closing:
            return
        if not self._flush_settings():
            return
        self.closing = True
        self._update_controls()
        self._tick()
        def finished(_):
            self.shutdown_complete = True
            self._apply_snapshot(self.controller.snapshot)
        self._run(self.controller.shutdown, finished, self._shutdown_failed)

    def _shutdown_failed(self, error):
        self.closing = False
        self.show_error(f'Could not finish shutting down: {error}. Keep {APP_NAME} open and stop the job before closing.')
        self._update_controls()


class RepeaterApplication(Adw.Application):
    def _release_model(self, *_):
        if self.window is not None:
            self.window.controller.shutdown()

    def __init__(self):
        GLib.set_application_name(APP_NAME)
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        Gtk.Window.set_default_icon_name(self.get_application_id())
        self.window = None
        self.connect('shutdown', self._release_model)
        for name, callback in (('new', lambda *_: self.window.show_recorder()),
                               ('settings', lambda *_: self.window.show_settings()),
                               ('refresh', lambda *_: self.window.refresh_history()),
                               ('quit', lambda *_: self.window.close())):
            action = Gio.SimpleAction.new(name, None)
            action.connect('activate', callback)
            self.add_action(action)
        self.set_accels_for_action('app.new', ['<Primary>n'])
        self.set_accels_for_action('app.settings', ['<Primary>comma'])
        self.set_accels_for_action('app.refresh', ['<Primary>r'])
        self.set_accels_for_action('app.quit', ['<Primary>q'])
        self.signal_sources = [
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, number, self._terminal_signal)
            for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
        ]

    def _terminal_signal(self):
        if self.window is None:
            self.quit()
        else:
            self.window.shutdown()
        return True

    def do_shutdown(self):
        for source in self.signal_sources:
            GLib.source_remove(source)
        Adw.Application.do_shutdown(self)

    def do_activate(self):
        if self.window is None:
            self.window = RepeaterWindow(self)
        self.window.present()


def main():
    if not Gtk.init_check():
        print(f'{APP_NAME} could not connect to a desktop display. Run it inside your GNOME/Wayland or X11 desktop session.', file=sys.stderr)
        return 1
    if not hasattr(Adw, 'NavigationSplitView') or not hasattr(Adw, 'AlertDialog'):
        print(f'{APP_NAME} needs libadwaita 1.5 or newer. Update libadwaita with your distribution package manager.', file=sys.stderr)
        return 1
    return RepeaterApplication().run(sys.argv)


if __name__ == '__main__':
    sys.exit(main())
