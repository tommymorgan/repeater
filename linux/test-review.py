"""Synthetic HTTP and on-disk review contracts; no recordings or personal settings."""

import copy
import http.server
import json
from pathlib import Path
import tempfile
import threading
import unittest

from review import (ReviewConfig, ReviewError, excerpt_interval, load_review,
                    record_decision, record_playback, review_findings, run_review)
from transcribe import (edit_transcript_text, load_transcript_edit_state, render_exports,
                        save_transcript_edit_state, undo_transcript_edit, redo_transcript_edit)


class ReviewTests(unittest.TestCase):
  def setUp(self):
    temporary = tempfile.TemporaryDirectory()
    self.addCleanup(temporary.cleanup)
    self.session = Path(temporary.name)
    self.payload = {'schemaVersion': 1, 'meetingId': 'synthetic-review', 'title': 'Synthetic',
      'segments': [{'source': 'me', 'speakerLabel': 'Me', 'text': 'Deploy the cash tomorrow.',
                    'startTime': 0.0, 'endTime': 3.0}]}
    (self.session / 'session.json').write_text(json.dumps({'id': 'synthetic-review', 'durationSeconds': 3.0}))
    self.write_payload(self.payload)
    self.output = {'findings': [{'segmentIndex': 0, 'quote': 'cash', 'reason': 'Possibly cache in this context.'}]}
    self.status = 200
    self.raw = None
    self.during_request = None
    self.requests = []
    owner = self

    class Handler(http.server.BaseHTTPRequestHandler):
      def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        owner.requests.append((self.path, json.loads(body)))
        if owner.during_request is not None:
          owner.during_request()
        self.send_response(owner.status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Location', 'https://example.com/v1/chat/completions')
        self.end_headers()
        response = owner.raw if owner.raw is not None else json.dumps({
          'choices': [{'message': {'content': json.dumps(owner.output)}}]}).encode()
        self.wfile.write(response)

      def log_message(self, *args):
        pass

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    self.addCleanup(server.server_close)
    self.addCleanup(thread.join)
    self.addCleanup(server.shutdown)
    self.config = ReviewConfig(f'http://127.0.0.1:{server.server_port}/v1', 'synthetic-local')

  def write_payload(self, payload):
    for name, text in render_exports(payload).items():
      (self.session / name).write_text(text)

  def state(self):
    return load_transcript_edit_state(self.session)

  def generate(self):
    return run_review(self.session, self.config)

  def test_http_request_is_text_only_and_findings_are_exact_unicode_anchors(self):
    self.payload['segments'][0]['text'] = 'Café cash tomorrow.'
    self.write_payload(self.payload)
    store = self.generate()
    finding = review_findings(store, self.state())[0]
    self.assertEqual((finding['startChar'], finding['endChar']), (5, 9))
    self.assertEqual(finding['runText'], 'Café cash tomorrow.')
    self.assertEqual(finding['originalAsr'], 'Café cash tomorrow.')
    self.assertEqual(finding['model'], 'synthetic-local')
    path, request = self.requests[0]
    self.assertEqual(path, '/v1/chat/completions')
    self.assertEqual(json.loads(request['messages'][1]['content']), {'segments': [
      {'segmentIndex': 0, 'text': 'Café cash tomorrow.'}]})
    self.assertEqual(load_review(self.session), store)

  def test_empty_findings_is_a_saved_successful_run(self):
    self.output = {'findings': []}
    store = self.generate()
    self.assertEqual(store['revision'], 1)
    self.assertEqual(len(store['runs']), 1)
    self.assertEqual(review_findings(store, self.state()), [])

  def test_untrusted_invalid_anchors_preserve_previous_history(self):
    previous = self.generate()
    invalid = [None, {'findings': None}, {'findings': [], 'replacement': 'unsafe'},
      {'findings': [{'segmentIndex': True, 'quote': 'cash', 'reason': 'x'}]},
      {'findings': [{'segmentIndex': 4, 'quote': 'cash', 'reason': 'x'}]},
      {'findings': [{'segmentIndex': 0, 'quote': 'absent', 'reason': 'x'}]},
      {'findings': [{'segmentIndex': 0, 'quote': 'cash', 'reason': ''}]},
      {'findings': [self.output['findings'][0]] * 2},
      {'findings': [self.output['findings'][0]] * 41}]
    for output in invalid:
      with self.subTest(output=output):
        self.output = output
        with self.assertRaises(ReviewError):
          self.generate()
        self.assertEqual(load_review(self.session), previous)

  def test_ambiguous_quote_is_rejected_even_when_occurrences_overlap(self):
    self.payload['segments'][0]['text'] = 'aaa'
    self.write_payload(self.payload)
    self.output['findings'][0]['quote'] = 'aa'
    with self.assertRaises(ReviewError):
      self.generate()
    self.assertEqual(load_review(self.session)['runs'], [])

  def test_redirect_errors_malformed_and_oversized_responses_never_publish(self):
    for status, raw in [(302, b''), (503, b''), (200, b'not JSON'), (200, b'x' * 65537),
                        (200, b'{"choices":[]}')]:
      with self.subTest(status=status, bytes=len(raw)):
        self.status, self.raw = status, raw
        with self.assertRaises(ReviewError):
          self.generate()
        self.assertEqual(load_review(self.session)['runs'], [])
    self.assertEqual(len(self.requests), 5)

  def test_public_credentials_and_unsafe_endpoints_are_rejected_before_http(self):
    for url in ['http://8.8.8.8/v1', 'https://user:secret@127.0.0.1/v1',
                'http://127.0.0.1/v1?key=secret', 'http://127.0.0.1/v1#fragment',
                'file:///v1', 'http://0.0.0.0/v1', 'http://127.0.0.1\\evil/v1']:
      with self.subTest(url=url), self.assertRaises(ReviewError):
        run_review(self.session, ReviewConfig(url, 'test'))
    self.assertEqual(self.requests, [])

  def test_saved_edit_during_http_rejects_stale_result_without_holding_lock(self):
    def change():
      state = edit_transcript_text(self.state(), 0, 'Deploy the cache tomorrow.')
      save_transcript_edit_state(self.session, state)
    self.during_request = change
    with self.assertRaisesRegex(ReviewError, 'changed during review'):
      self.generate()
    self.assertEqual(self.state().payload['segments'][0]['text'], 'Deploy the cache tomorrow.')
    self.assertEqual(load_review(self.session)['runs'], [])

  def test_cancelled_request_does_not_publish(self):
    cancel = threading.Event()
    self.during_request = cancel.set
    with self.assertRaisesRegex(ReviewError, 'cancelled'):
      run_review(self.session, self.config, cancel=cancel)
    self.assertEqual(load_review(self.session)['runs'], [])

  def test_edited_keep_records_current_text_and_undo_reopens_without_erasing_history(self):
    store = self.generate()
    finding = review_findings(store, self.state())[0]
    edited = edit_transcript_text(self.state(), 0, 'Deploy the cache tomorrow.')
    save_transcript_edit_state(self.session, edited)
    store = record_decision(self.session, finding['id'], 'keep', edited, store['revision'])
    decision = store['decisions'][0]
    self.assertEqual(decision['beforeText'], 'Deploy the cash tomorrow.')
    self.assertEqual(decision['afterText'], 'Deploy the cache tomorrow.')
    self.assertEqual(decision['originalAsr'], 'Deploy the cash tomorrow.')
    self.assertTrue(decision['changed'])
    self.assertTrue(review_findings(store, edited)[0]['resolved'])
    undone = undo_transcript_edit(edited)
    save_transcript_edit_state(self.session, undone)
    self.assertFalse(review_findings(load_review(self.session), undone)[0]['resolved'])
    redone = redo_transcript_edit(undone)
    save_transcript_edit_state(self.session, redone)
    self.assertTrue(review_findings(load_review(self.session), redone)[0]['resolved'])
    self.assertEqual(len(load_review(self.session)['decisions']), 1)

  def test_commit_needs_changed_text_and_skip_stays_unresolved(self):
    store = self.generate()
    finding = review_findings(store, self.state())[0]
    with self.assertRaises(ReviewError):
      record_decision(self.session, finding['id'], 'commit', self.state())
    store = record_decision(self.session, finding['id'], 'skip', self.state())
    self.assertFalse(review_findings(store, self.state())[0]['resolved'])
    edited = edit_transcript_text(self.state(), 0, 'Deploy the cache tomorrow.')
    with self.assertRaises(ReviewError):
      record_decision(self.session, finding['id'], 'commit', edited)
    save_transcript_edit_state(self.session, edited)
    with self.assertRaises(ReviewError):
      record_decision(self.session, finding['id'], 'commit', edited, 0)
    store = record_decision(self.session, finding['id'], 'commit', edited)
    self.assertTrue(review_findings(store, edited)[0]['resolved'])

  def test_rerun_and_retranscription_retain_old_events_but_invalidate_active_findings(self):
    first = self.generate()
    old_id = first['runs'][0]['findings'][0]['id']
    store = record_decision(self.session, old_id, 'keep', self.state())
    store = self.generate()
    self.assertEqual(len(store['runs']), 2)
    self.assertNotEqual(review_findings(store, self.state())[0]['id'], old_id)
    with self.assertRaises(ReviewError):
      record_decision(self.session, old_id, 'keep', self.state())
    new_payload = copy.deepcopy(self.payload)
    new_payload['segments'][0]['text'] = 'A new generated transcript.'
    # A fresh canonical export without correction history is a distinct ASR generation.
    self.write_payload(new_payload)
    self.assertEqual(review_findings(load_review(self.session), self.state()), [])
    self.assertEqual(len(load_review(self.session)['decisions']), 1)

  def test_word_excerpt_uses_real_times_and_legacy_or_edited_run_uses_whole_segment(self):
    self.payload['segments'][0]['words'] = [
      {'text': 'Deploy', 'startChar': 0, 'endChar': 6, 'startTime': 0.0, 'endTime': .4},
      {'text': 'the', 'startChar': 7, 'endChar': 10, 'startTime': .4, 'endTime': .8},
      {'text': 'cash', 'startChar': 11, 'endChar': 15, 'startTime': .8, 'endTime': 1.4},
      {'text': 'tomorrow.', 'startChar': 16, 'endChar': 25, 'startTime': 1.4, 'endTime': 3.0}]
    self.write_payload(self.payload)
    store = self.generate()
    finding = review_findings(store, self.state())[0]
    source, start, end, alignment = excerpt_interval(finding, self.state())
    self.assertEqual((source, alignment), ('me', 'word'))
    self.assertAlmostEqual(start, .62)
    self.assertAlmostEqual(end, 1.58)
    shifted = edit_transcript_text(self.state(), 0, 'Please deploy the cash tomorrow.')
    save_transcript_edit_state(self.session, shifted)
    shifted_store = self.generate()
    shifted_finding = review_findings(shifted_store, shifted)[0]
    self.assertEqual(shifted_finding['startChar'], 18)
    self.assertEqual(excerpt_interval(shifted_finding, shifted), ('me', start, end, 'word'))
    edited = edit_transcript_text(shifted, 0, 'Deploy the cache tomorrow.')
    save_transcript_edit_state(self.session, edited)
    self.output['findings'][0]['quote'] = 'cache'
    store = self.generate()
    self.assertEqual(excerpt_interval(review_findings(store, edited)[0], edited), ('me', 0.0, 3.0, 'segment'))

  def test_playback_events_do_not_resolve_findings_and_survive_restart(self):
    store = self.generate()
    finding = review_findings(store, self.state())[0]
    self.assertEqual(excerpt_interval(finding, self.state()), ('me', 0.0, 3.0, 'segment'))
    for event in ('started', 'stopped', 'started', 'completed', 'failed'):
      store = record_playback(self.session, finding['id'], event, 'me', 0.0, 3.0)
    self.assertEqual([event['event'] for event in load_review(self.session)['playback']],
                     ['started', 'stopped', 'started', 'completed', 'failed'])
    self.assertFalse(review_findings(store, self.state())[0]['resolved'])
    for source, start, end in [('them', 0, 3), ('me', -1, 3), ('me', 0, 4), ('me', 0, float('nan'))]:
      with self.assertRaises(ReviewError):
        record_playback(self.session, finding['id'], 'started', source, start, end)

  def test_symlink_history_is_not_read_or_overwritten(self):
    target = self.session / 'unrelated.json'
    target.write_text('untouched')
    (self.session / 'transcript-review.json').symlink_to(target)
    with self.assertRaises(ReviewError):
      self.generate()
    self.assertEqual(target.read_text(), 'untouched')

  def test_invalid_saved_history_is_preserved_not_reset(self):
    path = self.session / 'transcript-review.json'
    path.write_text('{"schemaVersion":99}')
    with self.assertRaises(ReviewError):
      load_review(self.session)
    self.assertEqual(path.read_text(), '{"schemaVersion":99}')

  def test_payload_limit_and_case_changed_quote_are_rejected(self):
    self.output['findings'][0]['quote'] = 'Cash'
    with self.assertRaises(ReviewError):
      self.generate()
    self.payload['segments'][0]['text'] = 'x' * 120001
    self.write_payload(self.payload)
    with self.assertRaisesRegex(ReviewError, 'too large'):
      self.generate()
    self.assertEqual(len(self.requests), 1)
    self.assertEqual(load_review(self.session)['runs'], [])

  def test_failed_publication_preserves_existing_history(self):
    from unittest.mock import patch
    previous = self.generate()
    with patch('transcribe.os.replace', side_effect=OSError('synthetic disk failure')):
      with self.assertRaises(ReviewError):
        self.generate()
    self.assertEqual(load_review(self.session), previous)

  def test_busy_recording_rejects_review_without_http_or_mutation(self):
    from transcribe import session_lock
    with session_lock(self.session):
      with self.assertRaisesRegex(ReviewError, 'busy'):
        self.generate()
    self.assertEqual(self.requests, [])
    self.assertEqual(load_review(self.session)['runs'], [])


if __name__ == '__main__':
  unittest.main()
