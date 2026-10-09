"""Native transcript reading views; Me/Them identify audio tracks, not speakers."""

import gi

gi.require_version('Gtk', '4.0')
from gi.repository import Gdk, GLib, Gtk, Pango
from transcribe import speaker_label


def _timestamp(seconds):
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours:02}:{minutes:02}:{seconds:02}' if hours else f'{minutes:02}:{seconds:02}'


def _label(text, style=None, wrap=False):
    widget = Gtk.Label(label=text, xalign=0, selectable=True, wrap=wrap)
    if wrap:
        widget.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        widget.set_width_chars(1)
        widget.set_max_width_chars(72)
        widget.set_hexpand(True)
    if style:
        widget.add_css_class(style)
    return widget


class TranscriptParagraph(Gtk.TextView):
    """Size a native editor to its wrapped paragraph rather than a scrollable page."""
    def do_get_request_mode(self):
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def do_measure(self, orientation, for_size):
        if orientation == Gtk.Orientation.HORIZONTAL:
            return 1, 1, -1, -1
        buffer = self.get_buffer()
        text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
        layout = self.create_pango_layout(text or ' ')
        layout.set_width(max(1, for_size if for_size >= 0 else 300) * Pango.SCALE)
        layout.set_wrap(Pango.WrapMode.WORD_CHAR)
        height = layout.get_pixel_size()[1]
        return height, height, -1, -1


class TranscriptPane(Gtk.Stack):
    def __init__(self, edited=None, undo=None, redo=None, play=None, stop_playback=None, speaker_action=None):
        super().__init__(hexpand=True, vexpand=True)
        self.play = play
        self.stop_playback = stop_playback
        self._play_buttons = {}
        self.edited = edited
        self.speaker_action = speaker_action
        self.voice_hints = {}
        self.undo = undo
        self.redo = redo
        self._buffers = {}
        self._editors = []
        self._previews = []
        self._review_slots = {}
        self._review_findings = []
        self.review_action = None
        self.review_play = None
        self._syncing = False
        provider = Gtk.CssProvider()
        provider.load_from_string('.transcript-paragraph, .transcript-paragraph text { background-color: transparent; }')
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.text = ''
        self.has_speech = False
        self._payload = None
        self._expanders = []
        self._timeline_segments = None
        self._timeline_built = False
        self._build_generation = 0
        self._review_enabled = True
        self.set_vhomogeneous(False)
        self.controls = Gtk.Box(spacing=8, halign=Gtk.Align.END, valign=Gtk.Align.CENTER)
        self.controls.set_visible(False)
        self.controls.update_property([Gtk.AccessibleProperty.LABEL], ['Transcript view controls'])

        self.undo_button = Gtk.Button(icon_name='edit-undo-symbolic', tooltip_text='Undo transcript correction')
        self.redo_button = Gtk.Button(icon_name='edit-redo-symbolic', tooltip_text='Redo transcript correction')
        for widget, name, callback in ((self.undo_button, 'Undo transcript correction', undo),
                                       (self.redo_button, 'Redo transcript correction', redo)):
            widget.update_property([Gtk.AccessibleProperty.LABEL], [name])
            widget.set_sensitive(False)
            widget.connect('clicked', lambda _button, action=callback: action() if action else None)
            self.controls.append(widget)
        self._expand_button = Gtk.Button()
        self._expand_button.add_css_class('flat')
        self._expand_labels = Gtk.Stack()
        self._expand_labels.add_named(Gtk.Label(label='Expand all'), 'expand')
        self._expand_labels.add_named(Gtk.Label(label='Collapse all'), 'collapse')
        self._expand_button.set_child(self._expand_labels)
        self._expand_button.connect('clicked', self._toggle_all)
        self._expand_slot = Gtk.Stack()
        self._expand_slot.add_named(self._expand_button, 'action')
        self._expand_slot.add_named(Gtk.Box(), 'blank')
        self.controls.append(self._expand_slot)

        segments = Gtk.Box(homogeneous=True)
        segments.add_css_class('linked')
        segments.set_size_request(176, -1)
        segments.update_property([Gtk.AccessibleProperty.LABEL], ['Transcript view'])
        self._thread_button = Gtk.ToggleButton(label='Thread')
        self._timeline_button = Gtk.ToggleButton(label='Timeline')
        self._timeline_button.set_group(self._thread_button)
        for name, widget in (('thread', self._thread_button), ('timeline', self._timeline_button)):
            widget.connect('toggled', self._mode_changed, name)
            segments.append(widget)
        self.controls.append(segments)

        self._thread = self._add_view('thread', 'Transcript conversation thread', boxed=True)
        self._timeline = self._add_view('timeline', 'Chronological transcript')
        self._thread_button.set_active(True)
        self._update_expand_caption()

    def _add_view(self, name, accessible_name, boxed=False):
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.START)
        if boxed:
            content.add_css_class('card')
        content.update_property([Gtk.AccessibleProperty.LABEL], [accessible_name])
        scroller = Gtk.ScrolledWindow(hexpand=True, vexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(content)
        self.add_named(scroller, name)
        return content

    def _mode_changed(self, widget, name):
        if widget.get_active():
            if self.stop_playback:
                self.stop_playback()
            self.set_visible_child_name(name)
            self._expand_slot.set_visible_child_name('action' if name == 'thread' else 'blank')
            if name == 'timeline':
                self._ensure_timeline()

    def _ensure_timeline(self):
        if self._timeline_built or self._timeline_segments is None:
            return
        self._timeline_built = True
        remaining = iter(self._timeline_segments)
        def pump():
            for _batch, segment in enumerate(remaining):
                self._append_timeline(segment)
                if _batch >= 24:
                    return GLib.SOURCE_CONTINUE
            return GLib.SOURCE_REMOVE
        GLib.idle_add(pump)

    def _update_expand_caption(self):
        expanded = bool(self._expanders) and all(row.get_expanded() for row in self._expanders)
        self._expand_labels.set_visible_child_name('collapse' if expanded else 'expand')
        self._expand_button.update_property(
            [Gtk.AccessibleProperty.LABEL], ['Collapse all turns' if expanded else 'Expand all turns'])

    def _toggle_all(self, _button):
        expand = not all(row.get_expanded() for row in self._expanders)
        for row in self._expanders:
            row.set_expanded(expand)
        self._update_expand_caption()

    def _heading(self, segments, snippet=False):
        first = segments[0]
        heading = Gtk.Grid(column_spacing=8, row_spacing=4, hexpand=True)
        text_column = 1 if snippet else 0
        title = Gtk.Box(spacing=8, hexpand=True)
        key = ('turn' if snippet else 'segment', tuple(segment['_index'] for segment in segments))
        start = min(segment['startTime'] for segment in segments)
        end = max(segment['endTime'] for segment in segments)
        name = speaker_label(self._payload, first, include_source=True)
        caption = f'Play {name} audio, {_timestamp(start)}–{_timestamp(end)}'
        play_button = Gtk.Button(icon_name='media-playback-start-symbolic', tooltip_text=caption)
        play_button.add_css_class('flat')
        play_button.update_property([Gtk.AccessibleProperty.LABEL], [caption])
        play_button.connect('clicked', lambda *_: self.play(first['source'], start, end, key) if self.play else None)
        play_button.set_sensitive(end > start)
        self._play_buttons[key] = (play_button, caption, end > start)
        title.append(self._speaker_menu(first, segments))
        title.append(_label(name, 'heading'))
        heading.attach(title, text_column, 0, 1, 1)

        preview = None
        if snippet:
            preview = _label(' '.join(segment['text'] for segment in segments))
            preview.set_text(' '.join(preview.get_text().split()))
            preview.set_ellipsize(Pango.EllipsizeMode.END)
            preview.set_width_chars(1)
            preview.set_hexpand(True)
            preview.add_css_class('dim-label')
            heading.attach(preview, text_column, 1, 1, 1)
        time_range = f'{_timestamp(first["startTime"])}–{_timestamp(max(s["endTime"] for s in segments))}'
        time = _label(time_range, 'dim-label')
        time.set_halign(Gtk.Align.END)
        time.set_hexpand(True)
        title.append(time)
        title.append(play_button)
        return heading, preview, f'{name}, {time_range}'
    def _speaker_menu(self, segment, segments):
        """Per-turn speaker controls: confirm (✓), reassign, rename-all."""
        if self._payload.get('schemaVersion') != 2 or not self.speaker_action:
            return Gtk.Box()
        speaker = next((entry for entry in self._payload['speakers']
                        if entry['id'] == segment.get('speakerId')), None)
        name = (speaker or {}).get('name') or (speaker or {}).get('label') or 'speaker'
        confirmed = segment.get('speakerConfirmed') is not None
        popover = Gtk.Popover()
        def build_once(*_):
            if popover.get_child() is not None:
                return
            column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                             margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)
            state = Gtk.Label(xalign=0, wrap=True)
            state.set_text(f'{"✓ Confirmed" if confirmed else "Not confirmed yet"} · {name} ({segment["source"]})')
            column.append(state)
            confirm = Gtk.Button(label='Unconfirm' if confirmed else '✓ Confirm speaker')
            confirm.connect('clicked', lambda *_: self.speaker_action('confirm', segment['_index'], None))
            column.append(confirm)
            self._hint_rows(segment, column)
            others = [entry for entry in self._payload['speakers']
                      if entry['source'] == segment['source'] and entry['id'] != segment.get('speakerId')]
            if others:
                column.append(_label('This snippet is someone else:', 'caption'))
                for entry in others:
                    choice = Gtk.Button(label=entry.get('name') or entry['label'])
                    choice.connect('clicked', lambda *_, sid=entry['id']:
                                   self.speaker_action('reassign', segment['_index'], sid))
                    column.append(choice)
            rename = Gtk.Button(label='Rename this speaker everywhere…')
            rename.connect('clicked', lambda *_: self.speaker_action('rename', segment['_index'], None))
            column.append(rename)
            popover.set_child(column)
        popover.connect('map', build_once)
        button = Gtk.MenuButton(icon_name='avatar-default-symbolic', tooltip_text=f'Confirm or edit {name}')
        button.set_popover(popover)
        button.add_css_class('flat')
        if confirmed:
            button.add_css_class('suggested-action')
        return button

    def set_voice_hints(self, hints):
        """Show tiered voice matches: {speakerId: {'name','score','tier','profileId'}}."""
        self.voice_hints = hints or {}

    def _hint_rows(self, segment, column):
        hint = self.voice_hints.get(segment.get('speakerId'))
        if not hint or segment.get('speakerConfirmed'):
            return
        score = hint['score']
        if hint['tier'] == 'auto':
            column.append(_label(f'Voice match: {hint["name"]} (score {score:.2f}) — this speaker is a remembered voice', 'caption', wrap=True))
        else:
            row = Gtk.Box(spacing=6)
            row.append(_label(f'Sounds like {hint["name"]} (score {score:.2f})', 'caption'))
            yes = Gtk.Button(label='That’s right')
            yes.add_css_class('suggested-action')
            yes.connect('clicked', lambda *_: self.speaker_action('hint-yes', segment['_index'], hint['profileId']))
            no = Gtk.Button(label='Not them')
            no.connect('clicked', lambda *_: self.speaker_action('hint-no', segment['_index'], hint['profileId']))
            row.append(yes)
            row.append(no)
            column.append(row)

    def _append_thread(self, segments, expanded):
        if self._thread.get_first_child():
            self._thread.append(Gtk.Separator())
        heading, preview, name = self._heading(segments, snippet=True)
        heading.set_margin_start(12)
        heading.set_margin_end(12)
        heading.set_margin_top(12)
        heading.set_margin_bottom(12)
        expander = Gtk.Expander(valign=Gtk.Align.CENTER)
        expander.update_property([Gtk.AccessibleProperty.LABEL], [name])
        heading.attach(expander, 0, 0, 1, 1)
        paragraphs = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                             margin_top=8, margin_bottom=4, hexpand=True)
        for segment in segments:
            paragraphs.append(self._paragraph(segment))
        heading.attach(paragraphs, 1, 2, 1, 1)
        expander.set_expanded(expanded)
        preview.set_visible(not expanded)
        paragraphs.set_visible(expanded)
        def changed(row, _property):
            preview.set_visible(not row.get_expanded())
            paragraphs.set_visible(row.get_expanded())
            self._update_expand_caption()
        expander.connect('notify::expanded', changed)
        self._expanders.append(expander)
        self._previews.append((preview, segments))
        self._thread.append(heading)

    def _append_timeline(self, segment):
        if self._timeline.get_first_child():
            self._timeline.append(Gtk.Separator())
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                      margin_top=16, margin_bottom=16, margin_start=12, margin_end=12)
        heading, _preview, _name = self._heading([segment])
        row.append(heading)
        row.append(self._paragraph(segment))
        self._timeline.append(row)

    def set_transcript(self, payload: dict, text: str) -> None:
        self.text = text
        if self._payload is not None and self._shape(payload) == self._shape(self._payload):
            self._syncing = True
            for index, segment in enumerate(payload['segments']):
                buffer = self._buffers[index]
                current = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
                if current != segment['text']:
                    insert = buffer.get_iter_at_mark(buffer.get_insert()).get_offset()
                    bound = buffer.get_iter_at_mark(buffer.get_selection_bound()).get_offset()
                    buffer.set_text(segment['text'])
                    length = buffer.get_char_count()
                    buffer.select_range(buffer.get_iter_at_offset(min(insert, length)),
                                        buffer.get_iter_at_offset(min(bound, length)))
            self._syncing = False
            self._payload = payload
            self._update_previews()
            return
        if payload == self._payload:
            return
        self.clear()
        self.text = text
        self._payload = payload
        segments = [dict(segment, _index=index) for index, segment in enumerate(payload['segments'])]
        segments.sort(key=lambda s: (s['startTime'], s['endTime'], s['source']))
        self._timeline_segments = segments
        self._timeline_built = False
        turns = []
        for segment in segments:
            previous = turns[-1][-1] if turns else None
            same_speaker = (payload['schemaVersion'] == 1 or
                            (segment.get('speakerId') is not None and previous is not None and
                             previous.get('speakerId') == segment['speakerId'] and
                             segment['startTime'] <= previous['endTime'] + 1))
            if previous is not None and previous['source'] == segment['source'] and same_speaker:
                turns[-1].append(segment)
            else:
                turns.append([segment])
        self._build_generation += 1
        generation = self._build_generation
        for index, turn in enumerate(turns[:40]):
            self._append_thread(turn, index == len(turns) - 1)
        self.has_speech = bool(segments)
        self.controls.set_visible(self.has_speech)
        self._update_expand_caption()
        remaining = iter(turns[40:])
        def pump():
            if generation != self._build_generation:
                return GLib.SOURCE_REMOVE
            for _batch in range(25):
                turn = next(remaining, None)
                if turn is None:
                    if self._review_findings:
                        self._render_review()
                    return GLib.SOURCE_REMOVE
                self._append_thread(turn, False)
            return GLib.SOURCE_CONTINUE
        if len(turns) > 40:
            GLib.idle_add(pump)

    @staticmethod
    def _shape(payload):
        return (payload.get('generation'), payload.get('speakers'),
                [(s['source'], s.get('speakerId'), s.get('speakerState'), s.get('speakerLabel'),
                  s['startTime'], s['endTime']) for s in payload['segments']])

    def _update_previews(self):
        for preview, segments in self._previews:
            preview.set_text(' '.join(' '.join(self._payload['segments'][s['_index']]['text'].split())
                                      for s in segments))

    def set_history(self, state):
        self.undo_button.set_sensitive(state is not None and state.can_undo)
        self.redo_button.set_sensitive(state is not None and state.can_redo)

    def set_playback(self, active_key):
        for key, (widget, caption, _valid) in self._play_buttons.items():
            active = key == active_key
            widget.set_icon_name('media-playback-stop-symbolic' if active else 'media-playback-start-symbolic')
            name = caption.replace('Play ', 'Stop ', 1) if active else caption
            widget.set_tooltip_text(name)
            widget.update_property([Gtk.AccessibleProperty.LABEL], [name])

    def set_playback_enabled(self, enabled):
        for widget, _caption, valid in self._play_buttons.values():
            widget.set_sensitive(enabled and valid)

    def set_editable(self, editable):
        for editor in self._editors:
            editor.set_editable(editable)
        self.undo_button.set_sensitive(editable and self.undo_button.get_sensitive())
        self.redo_button.set_sensitive(editable and self.redo_button.get_sensitive())

    def _paragraph(self, segment):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(self._editor(segment))
        slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(slot)
        self._review_slots.setdefault(segment['_index'], []).append(slot)
        return box

    def set_review(self, findings, action=None, play=None, enabled=True):
        new_findings = [f['id'] for f in findings] != [f['id'] for f in self._review_findings]
        self._review_findings = findings
        self._review_enabled = enabled
        self.review_action, self.review_play = action, play
        self._render_review()
        if findings and new_findings:
            for expander in self._expanders:
                expander.set_expanded(True)

    def _render_review(self):
        findings = self._review_findings
        action, play, enabled = self.review_action, self.review_play, self._review_enabled
        for index, buffer in self._buffers.items():
            tag = buffer.get_tag_table().lookup('review-suspect')
            if tag is None:
                tag = buffer.create_tag('review-suspect', underline=Pango.Underline.ERROR)
            buffer.remove_tag(tag, buffer.get_start_iter(), buffer.get_end_iter())
            text = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)
            for slot in self._review_slots.get(index, []):
                while child := slot.get_first_child():
                    slot.remove(child)
            for finding in (f for f in findings if f['segmentIndex'] == index):
                if not finding['resolved'] and text.count(finding['quote']) == 1:
                    start = text.index(finding['quote'])
                    buffer.apply_tag(tag, buffer.get_iter_at_offset(start),
                                     buffer.get_iter_at_offset(start + len(finding['quote'])))
                for slot in self._review_slots.get(index, []):
                    status = (finding.get('decision') if finding['resolved'] else
                              'Skipped · unresolved' if finding.get('decision') == 'skip' else 'Needs review')
                    reason = _label(f"{status} · {finding['reason']}", 'caption', wrap=True)
                    reason.set_tooltip_text(f"Model: {finding['model']} · Run: {finding['runId']}")
                    slot.append(reason)
                    actions = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE,
                                          min_children_per_line=1, max_children_per_line=3,
                                          column_spacing=4, row_spacing=4, homogeneous=False)
                    alignment = finding.get('alignment', 'segment')
                    play_caption = 'Play excerpt' if alignment == 'word' else 'Play whole clip'
                    if finding.get('playing_excerpt'):
                        play_caption = 'Stop excerpt' if alignment == 'word' else 'Stop whole clip'
                    choices = [(play_caption, 'play'),
                               ('Stop context' if finding.get('playing_context') else 'Whole clip context', 'context'),
                               ('Keep', 'keep'), ('Commit edit', 'commit'), ('Skip', 'skip')]
                    for caption, choice in choices:
                        widget = Gtk.Button(label=caption)
                        widget.set_sensitive(enabled and (choice != 'commit' or text != finding['runText']))
                        if choice in ('play', 'context'):
                            widget.connect('clicked', lambda _w, f=finding, c=choice: play(f, c == 'context') if play else None)
                        else:
                            widget.connect('clicked', lambda _w, f=finding, c=choice: action(f['id'], c) if action else None)
                        actions.insert(widget, -1)
                    slot.append(actions)
                    if alignment != 'word':
                        slot.append(_label('Word timing unavailable', 'dim-label', wrap=True))

    def _editor(self, segment):
        index = segment['_index']
        buffer = self._buffers.get(index)
        if buffer is None:
            buffer = Gtk.TextBuffer()
            buffer.set_enable_undo(False)
            buffer.set_text(segment['text'])
            buffer.connect('changed', self._buffer_changed, index)
            self._buffers[index] = buffer
        view = TranscriptParagraph(buffer=buffer, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                            hexpand=True, editable=True, accepts_tab=False)
        view.set_vexpand(False)
        view.set_valign(Gtk.Align.START)
        view.add_css_class('transcript-paragraph')
        view.update_property([Gtk.AccessibleProperty.LABEL],
                             [f"{speaker_label(self._payload, segment, include_source=True)} at {_timestamp(segment['startTime'])}, editable transcript"])
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect('key-pressed', self._editor_key)
        view.add_controller(keys)
        buffer.connect('changed', lambda _buffer: view.queue_resize())
        self._editors.append(view)
        return view

    def _buffer_changed(self, buffer, index):
        if not self._syncing and self.edited:
            self.edited(index, buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True))

    def _editor_key(self, _controller, key, _code, modifiers):
        if not modifiers & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.META_MASK):
            return False
        if key in (Gdk.KEY_z, Gdk.KEY_Z):
            action = self.redo if modifiers & Gdk.ModifierType.SHIFT_MASK else self.undo
        elif key in (Gdk.KEY_y, Gdk.KEY_Y):
            action = self.redo
        else:
            return False
        if action:
            action()
        return True

    def clear(self) -> None:
        self.text = ''
        self.has_speech = False
        self._payload = None
        self._timeline_segments = None
        self._timeline_built = False
        self._build_generation += 1
        self._buffers.clear()
        self._editors.clear()
        self._play_buttons.clear()
        self._previews.clear()
        self._review_slots.clear()
        for content in (self._thread, self._timeline):
            while child := content.get_first_child():
                content.remove(child)
        self.controls.set_visible(False)
        self._thread_button.set_active(True)
        self._update_expand_caption()
