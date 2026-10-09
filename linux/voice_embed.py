#!/usr/bin/env python3
"""Embed 16 kHz windows with pinned TitaNet; run under linux/.build/linux/nemotron-venv.

Reads {"model": path, "windows": [{"wav", "start", "length"}]} from the JSON file
named on argv[1]; writes {"embeddings": [[...]]} to that file's .out sibling
(NeMo prints warnings on stdout, so stdout stays unparsed). Local-only.
"""

import json
import sys

import numpy as np
import soundfile as sf
import torch


def main(argv):
  if len(argv) != 2:
    print('usage: voice_embed.py REQUEST.json', file=sys.stderr)
    return 2
  request = json.load(open(argv[1], encoding='utf-8'))
  sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent / '.build/linux/nemo-speech'))
  from nemo.collections.asr.models import EncDecSpeakerLabelModel
  model = EncDecSpeakerLabelModel.restore_from(request['model'])
  device = 'cuda' if torch.cuda.is_available() else 'cpu'
  model = model.to(device).eval()
  embeddings = []
  with torch.inference_mode():
    for window in request['windows']:
      audio, rate = sf.read(window['wav'], start=int(window['start'] * 16000),
                            frames=int(window['length'] * 16000), dtype='float32', always_2d=False)
      if rate != 16000 or len(audio) != int(window['length'] * 16000):
        raise SystemExit(f'bad window: {window["wav"]}@{window["start"]} rate={rate} len={len(audio)}')
      signal = torch.from_numpy(np.ascontiguousarray(audio)).to(device)
      length = torch.tensor([len(audio)], device=device)
      _, embedded = model(input_signal=signal.unsqueeze(0), input_signal_length=length)
      vector = embedded.squeeze().detach().cpu().numpy().astype(float)
      norm = float(np.linalg.norm(vector))
      embeddings.append((vector / norm).tolist())
  json.dump({'embeddings': embeddings}, open(argv[1] + '.out', 'w'))
  return 0


if __name__ == '__main__':
  raise SystemExit(main(sys.argv))
