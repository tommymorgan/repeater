#!/usr/bin/env python3
"""Local-only NeMo coupled eight-stream masked ASR. No model or device fallback."""
import argparse
import contextlib
import copy
import ctypes
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import resource
import signal
import sys
import tempfile
import time
import unicodedata
import wave

SPEECH_COMMIT = '50c71dbe1534a89e0d07fe66fbffd205408062ad'
ASR_REVISION = 'ea30d66debe3740a08b573244286791d423d6b3e'
DIAR_REVISION = 'f667ed73aee57d40cc39428eb768b4fd87a0a29e'
ASR_HASH = '210214ed94039bf6bfbb9a047c7fa289628db75b103e2bf6381fa78285436a74'
DIAR_HASH = '867c53f552998f772e5b5e5c082962ae85ee7ca5669c2bc17d7f615133d4e96d'
TOKENIZER_HASH = 'ce3895e40806f02a26c3a225161b96ef682d6c0054bae32a245dec4258d7d291'
LOCALES = ('en-US en-GB es-US es-ES fr-FR fr-CA it-IT pt-BR pt-PT nl-NL de-DE '
           'tr-TR ru-RU ar-AR hi-IN ja-JP ko-KR vi-VN uk-UA pl-PL sv-SE cs-CZ '
           'nb-NO da-DK bg-BG fi-FI hr-HR sk-SK zh-CN hu-HU ro-RO et-EE').split()
ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / '.build/linux'


def checkpoint(path, expected):
    path = Path(path).resolve(strict=True)
    if path.suffix != '.nemo' or not path.is_file():
        raise ValueError(f'Expected a local pinned .nemo checkpoint, not GGUF: {path}')
    with path.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    if digest != expected:
        raise ValueError(f'Wrong checkpoint SHA256 for {path}; run linux/setup-nemotron.sh')
    return path


def locale(value):
    aliases = {code.split('-')[0]: code for code in reversed(LOCALES)}
    aliases['no'] = 'nb-NO'
    result = aliases.get(value, value)
    if result not in LOCALES:
        raise ValueError(f'Unsupported pretrained language {value!r}. Supported locales: {", ".join(LOCALES)}')
    return result


def audio_duration(path):
    with wave.open(str(path), 'rb') as audio:
        if (audio.getnchannels(), audio.getframerate(), audio.getsampwidth(), audio.getcomptype()) != (1, 16000, 2, 'NONE'):
            raise ValueError('Audio must be mono 16000 Hz PCM16 WAV; convert with ffmpeg first')
        return audio.getnframes() / 16000


def words_from_tokens(text, ids, intervals, decode, duration):
    """Map decoder token emissions to exact Unicode codepoint spans, never interpolate."""
    if len(ids) != len(intervals) or not ids:
        return None
    char_times = []
    previous = ''
    for index in range(len(ids)):
        current = decode(ids[:index + 1]).strip()
        if current.startswith(previous):
            char_times.extend([intervals[index]] * (len(current) - len(previous)))
        else:
            common = next((i for i, (left, right) in enumerate(zip(previous, current)) if left != right),
                          min(len(previous), len(current)))
            if '�' not in previous[common:]:
                return None
            byte_times = char_times[common:] + [intervals[index]]
            if any(pair is None for pair in byte_times):
                return None
            interval = (min(pair[0] for pair in byte_times), max(pair[1] for pair in byte_times))
            char_times[common:] = [interval] * (len(current) - common)
        previous = current
    if previous != text or len(char_times) != len(text):
        return None
    words = []
    for match in re.finditer(r'\S+', text):
        # Punctuation can be emitted on the next utterance; it has no spoken duration.
        times = [char_times[i] for i in range(match.start(), match.end())
                 if text[i].isalnum() or unicodedata.category(text[i]).startswith('M')]
        if not times or any(pair is None for pair in times):
            return None
        start = min(pair[0] for pair in times)
        end = max(pair[1] for pair in times)
        if not math.isfinite(start + end) or start < -0.08 or start >= duration or end <= start:
            return None
        words.append({'text': match.group(), 'startTime': max(0.0, start),
                      'endTime': min(duration, end), 'startChar': match.start(), 'endChar': match.end()})
    return words


def segments_from_words(text, words, speaker):
    groups = []
    for word in words:
        if not groups or word['startTime'] > groups[-1][-1]['endTime'] + 1.0:
            groups.append([])
        groups[-1].append(word)
    result = []
    for group in groups:
        first, last = group[0]['startChar'], group[-1]['endChar']
        result.append({'text': text[first:last], 'startTime': min(w['startTime'] for w in group),
                       'endTime': max(w['endTime'] for w in group), 'speakerTag': speaker + 1,
                       'words': [{**w, 'startChar': w['startChar'] - first, 'endChar': w['endChar'] - first} for w in group]})
    return result


def load_models(args):
    if sys.version_info[:3] != (3, 13, 14):
        raise RuntimeError('Use .build/linux/nemotron-venv/bin/python (pinned Python 3.13.14)')
    source_root = BUILD / 'nemo-speech'
    utility = source_root / 'nemo/collections/asr/parts/utils/multispk_transcribe_utils.py'
    with utility.open('rb') as source:
        if hashlib.file_digest(source, 'sha256').hexdigest() != '7489b81a4e6db680942e7a08d0e8c775675602a1f1fd2cc62bcff6d7c704fb5c':
            raise RuntimeError('Wrong NeMo coupled runtime; run linux/setup-nemotron.sh')
    sys.path.insert(0, str(source_root))
    import torch
    if torch.__version__ != '2.12.0+cu132':
        raise RuntimeError('Wrong Torch runtime; run linux/setup-nemotron.sh for pinned 2.12.0+cu132')
    from omegaconf import OmegaConf
    from nemo.collections.asr.models import ASRModel
    from nemo.collections.asr.parts.submodules.subsampling import FeatureStacking
    from nemo.collections.asr.parts.utils.streaming_utils import CacheAwareStreamingAudioBuffer

    torch.set_num_threads(args.threads)
    device = torch.device('cuda:0' if args.backend == 'gpu' else 'cpu')
    if args.backend == 'gpu':
        if not torch.cuda.is_available():
            raise RuntimeError('GPU requested but CUDA is unavailable; install the pinned CUDA runtime or explicitly select CPU')
        torch.ones(1, device=device).mul_(2)
        torch.cuda.synchronize()
        torch.cuda.reset_peak_memory_stats()
    example = BUILD / 'nemo-speech/examples/asr/asr_cache_aware_streaming/speech_to_text_multitalker_streaming_infer.py'
    with example.open('rb') as source:
        if hashlib.file_digest(source, 'sha256').hexdigest() != '7c34077ce6f4286e5ad3f390994683ef957d965bb93e05dea1a51fb2c8b0b56e':
            raise RuntimeError('Wrong NeMo coupled driver; run linux/setup-nemotron.sh')
    spec = importlib.util.spec_from_file_location('parrot_coupled_nemo', example)
    driver = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = driver
    spec.loader.exec_module(driver)
    cfg = OmegaConf.structured(driver.MultitalkerTranscriptionConfig())
    cfg.merge_with({'asr_model': str(args.model), 'diar_model': str(args.diarizer),
                    'audio_file': None, 'device': str(device), 'batch_size': 1,
                    'num_workers': 0, 'max_num_of_spks': 8, 'single_speaker_mode': False,
                    'masked_asr': True, 'parallel_speaker_strategy': True, 'cache_gating': True,
                    'binary_diar_preds': True, 'att_context_size': [56, 13], 'fifo_len': 264,
                    'spkcache_update_period': 222, 'target_lang': args.language,
                    'strip_lang_tags': True, 'generate_realtime_scripts': False,
                    'precision': '32', 'use_amp': False})
    diar = driver.load_diar_model(str(args.diarizer), device).to(device).eval()
    asr = ASRModel.restore_from(str(args.model), map_location=device).to(device).eval()
    if not hasattr(asr, 'set_inference_prompt'):
        raise RuntimeError('Checkpoint does not expose Nemotron 3.5 language conditioning')
    if diar.sortformer_modules.n_spk != 8:
        raise RuntimeError('Diarization checkpoint is not eight-speaker V3')
    asr.encoder.set_default_att_context_size(att_context_size=cfg.att_context_size)
    driver.configure_asr_for_multitalker_streaming(cfg, asr)
    driver.validate_feature_frame_strides(asr_model=asr, diar_model=diar)
    driver.configure_diar_streaming(diar_model=diar, cfg=cfg,
        output_subsampling_factor=asr.encoder.subsampling_factor,
        diar_chunk_len=asr.encoder.streaming_cfg.valid_out_len + asr.encoder.streaming_cfg.cache_drop_size)
    cfg.pad_and_drop_preencoded = isinstance(diar.encoder.pre_encode, FeatureStacking)
    return torch, driver, cfg, asr, diar, CacheAwareStreamingAudioBuffer


def infer(args, duration):
    return infer_loaded(args, duration, load_models(args))


def infer_loaded(args, duration, models):
    torch, driver, cfg, asr, diar, CacheAwareStreamingAudioBuffer = models
    cfg = copy.deepcopy(cfg)
    cfg.audio_file = str(args.audio)
    if args.backend == 'gpu':
        torch.cuda.reset_peak_memory_stats()
    buffer = CacheAwareStreamingAudioBuffer(model=asr, online_normalization=False,
                                           pad_and_drop_preencoded=cfg.pad_and_drop_preencoded)
    buffer.append_audio_file(audio_filepath=str(args.audio), stream_id=-1)
    streamer = driver.SpeakerTaggedASR(cfg, asr, diar)
    manager = streamer.instance_manager
    original_update = manager.update_seglsts
    emissions = {}
    final_hypotheses = {}

    def capture(offset):
        for state in manager.batch_asr_states:
            for speaker in state.get_speakers():
                hyp = state.previous_hypothesis[speaker]
                if hyp is None:
                    continue
                final_hypotheses[speaker] = hyp
                ids = hyp.y_sequence.tolist() if hasattr(hyp.y_sequence, 'tolist') else list(hyp.y_sequence)
                frames = hyp.timestamp
                saved = emissions.setdefault(speaker, [])
                if len(frames) != len(ids) or hyp.dec_state is None:
                    saved.extend([None] * max(0, len(ids) - len(saved)))
                    continue
                previous_length = state._prev_decoded_lengths[speaker]
                for index in range(len(saved), len(ids)):
                    start = offset + (float(frames[index]) - previous_length) * state._frame_len_sec
                    saved.append((start, start + state._frame_len_sec))
        original_update(offset)

    manager.update_seglsts = capture
    right_offset = cfg.diar_right_context * diar.encoder.subsampling_factor
    try:
        with torch.inference_mode():
            for step, (chunk, lengths, diar_chunk, diar_lengths) in enumerate(buffer.iter_with_right_context(right_offset)):
                streamer.perform_parallel_streaming_stt_spk(step_num=step, chunk_audio=chunk,
                    chunk_lengths=lengths, diar_chunk_audio=diar_chunk, diar_chunk_lengths=diar_lengths,
                    is_buffer_empty=buffer.is_buffer_empty(),
                    drop_extra_pre_encoded=0 if step == 0 and not cfg.pad_and_drop_preencoded else asr.encoder.streaming_cfg.drop_extra_pre_encoded)
    finally:
        del manager.update_seglsts
    segments = []
    for speaker, hyp in final_hypotheses.items():
        text = (hyp.text or '').strip()
        if not text:
            continue
        ids = hyp.y_sequence.tolist() if hasattr(hyp.y_sequence, 'tolist') else list(hyp.y_sequence)
        words = words_from_tokens(text, ids, emissions[speaker], asr.decoding.decode_tokens_to_str_with_strip_punctuation, duration)
        if words:
            segments.extend(segments_from_words(text, words, speaker))
        else:
            print(f'Speaker {speaker + 1}: decoder word timing unavailable; preserving recognized text without words', file=sys.stderr)
            for state in manager.batch_asr_states:
                segments.extend({'text': seg['words'], 'startTime': max(0, min(duration, seg['start_time'])),
                                 'endTime': max(0, min(duration, seg['end_time'])), 'speakerTag': speaker + 1}
                                for seg in state.seglsts if seg['speaker'] == f'speaker_{speaker}' and seg['words'].strip())
    turns = []
    preds = manager.diar_states.diar_pred_out_stream[0].detach().cpu()
    frame_seconds = asr.cfg.preprocessor.window_stride * asr.encoder.subsampling_factor
    for speaker in range(8):
        start = None
        for index in range(len(preds) + 1):
            active = index < len(preds) and float(preds[index, speaker]) > 0.5
            if active and start is None:
                start = index * frame_seconds
            elif not active and start is not None:
                end = min(duration, index * frame_seconds)
                if start < end:
                    turns.append({'startTime': start, 'endTime': end, 'speakerTag': speaker + 1})
                start = None
    segments.sort(key=lambda seg: (seg['startTime'], seg['speakerTag']))
    return {'schemaVersion': 1, 'engine': 'nemotron-3.5', 'speakerCapacity': 8,
            'durationSeconds': duration, 'segments': segments,
            'speakerTurns': sorted(turns, key=lambda turn: (turn['startTime'], turn['speakerTag'])),
            'provenance': {'runtimeVersion': f'NeMo-Speech@{SPEECH_COMMIT}', 'modelRevision': ASR_REVISION,
                           'diarizerRevision': DIAR_REVISION, 'modelSha256': ASR_HASH,
                           'diarizerSha256': DIAR_HASH, 'backend': args.backend,
                           'tokenizerSha256': TOKENIZER_HASH,
                           'pythonVersion': '.'.join(map(str, sys.version_info[:3])),
                           'torchVersion': torch.__version__, 'cudaVersion': torch.version.cuda,
                           'timingOrigin': 'per-speaker-rnnt-token-emission', 'language': args.language,
                           'maskedAsr': True, 'parallelSpeakerStrategy': True,
                           'wordFrameSeconds': 0.08, 'speakerActivityFrameSeconds': frame_seconds,
                           'peakCudaAllocatedBytes': torch.cuda.max_memory_allocated() if args.backend == 'gpu' else 0,
                           'peakCudaReservedBytes': torch.cuda.max_memory_reserved() if args.backend == 'gpu' else 0}}


def bind_worker_parent(parent):
    if sys.platform != 'linux' or parent <= 1:
        raise ValueError('--worker-parent requires a live Linux parent PID greater than 1')
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    if os.getppid() != parent:
        raise RuntimeError('Worker parent changed during startup')


def read_worker_field(source):
    value = bytearray()
    while True:
        byte = source.read(1)
        if byte == b'\x00':
            return bytes(value)
        if not byte:
            if value:
                raise ValueError('Incomplete worker request at stdin EOF')
            return None
        value.extend(byte)


def write_result(result, destination, started):
    result['provenance'].update({'wallSeconds': time.monotonic() - started,
                                 'peakRssKiB': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss})
    with tempfile.NamedTemporaryFile(mode='w', dir=destination.parent, prefix=f'.{destination.name}.',
                                     encoding='utf-8', delete=False) as output:
        temporary = Path(output.name)
        try:
            json.dump(result, output, ensure_ascii=False, allow_nan=False)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)


def run_worker(args, protocol):
    models = load_models(args)
    try:
        print('ready', file=protocol, flush=True)
        while True:
            audio = read_worker_field(sys.stdin.buffer)
            if audio is None:
                return
            output = read_worker_field(sys.stdin.buffer)
            if output is None:
                raise ValueError('Missing output field at stdin EOF')
            started = time.monotonic()
            try:
                if not audio or not output:
                    raise ValueError('Worker audio and output paths must not be empty')
                request = argparse.Namespace(**vars(args))
                request.audio = Path(audio.decode('utf-8', errors='strict'))
                request.output = Path(output.decode('utf-8', errors='strict'))
                duration = audio_duration(request.audio)
                result = infer_loaded(request, duration, models)
                write_result(result, request.output, started)
            except Exception as error:
                print(f'Nemotron transcription failed: {error}', file=sys.stderr)
                print('error', file=protocol, flush=True)
            else:
                print('ok', file=protocol, flush=True)
    finally:
        del models


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('model', 'diarizer'):
        parser.add_argument(f'--{name}', required=True, type=Path)
    for name in ('audio', 'output'):
        parser.add_argument(f'--{name}', type=Path)
    parser.add_argument('--worker-parent', type=int)
    parser.add_argument('--language', default='en')
    parser.add_argument('--threads', type=int, default=4)
    parser.add_argument('--backend', choices=('cpu', 'gpu'), required=True)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    started = time.monotonic()
    protocol = None
    try:
        if args.worker_parent is not None:
            bind_worker_parent(args.worker_parent)
            protocol = os.fdopen(os.dup(sys.stdout.fileno()), 'w', encoding='utf-8', buffering=1)
            sys.stdout.flush()
            os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
            if args.audio is not None or args.output is not None:
                raise ValueError('Worker mode takes audio and output only from stdin')
        elif args.audio is None or args.output is None:
            raise ValueError('One-shot mode requires --audio and --output')
        if args.threads < 1:
            raise ValueError('--threads must be positive')
        args.language = locale(args.language)
        args.model = checkpoint(args.model, ASR_HASH)
        args.diarizer = checkpoint(args.diarizer, DIAR_HASH)
        os.environ['HF_HUB_OFFLINE'] = '1'
        os.environ['TRANSFORMERS_OFFLINE'] = '1'
        for variable, directory in (('HF_HOME', 'hf-cache'), ('TORCH_HOME', 'torch-cache'),
                                    ('MPLCONFIGDIR', 'matplotlib-cache'), ('NEMO_CACHE_DIR', 'nemo-cache')):
            os.environ[variable] = str(BUILD / directory)
        if protocol is not None:
            run_worker(args, protocol)
        else:
            duration = audio_duration(args.audio)
            with contextlib.redirect_stdout(sys.stderr):
                result = infer(args, duration)
            write_result(result, args.output, started)
    except (Exception, KeyboardInterrupt) as error:
        print(f'Nemotron transcription failed: {error}', file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 1
    finally:
        if protocol is not None:
            protocol.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
