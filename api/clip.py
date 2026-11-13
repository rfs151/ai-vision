"""Chinese-CLIP on OpenVINO (scripts/export_clip.py): crops and descriptions -> comparable vectors.

The image and the text encoder map into one space: a crop of a person in a green coat and the
text "穿绿色外套的人" give vectors with a high cosine. Used by api/search.py: every sighting a
worker uploads gets an image vector (once), every search one text vector. No torch/transformers
at runtime: the BERT tokenizer below reproduces transformers' BertTokenizer (checked by the export
script), preprocessing is a few lines of OpenCV. Models load on first use, so the API starts
as fast as before and runs without them (search then ignores the appearance condition).
"""
import json
import logging
import threading
import unicodedata
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger("clip")
CONTEXT = 52                    # Chinese-CLIP's text length (tokens, with [CLS] and [SEP])
MODEL_DIR = Path(__file__).resolve().parent.parent / "worker" / "models" / "chinese-clip-vit-b16"


def _is_cjk(cp):
    return (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or 0x20000 <= cp <= 0x2A6DF
            or 0x2A700 <= cp <= 0x2CEAF or 0xF900 <= cp <= 0xFAFF or 0x2F800 <= cp <= 0x2FA1F)


def _is_punct(ch):
    cp = ord(ch)
    if 33 <= cp <= 47 or 58 <= cp <= 64 or 91 <= cp <= 96 or 123 <= cp <= 126:
        return True
    return unicodedata.category(ch).startswith("P")


class BertTokenizer:
    """Lower-casing BERT WordPiece tokenizer (Chinese BERT vocab): CJK characters are one token each."""

    def __init__(self, vocab_path):
        words = Path(vocab_path).read_text(encoding="utf-8").split("\n")
        self.vocab = {w: i for i, w in enumerate(words) if w}
        self.unk, self.cls, self.sep = self.vocab["[UNK]"], self.vocab["[CLS]"], self.vocab["[SEP]"]

    def _basic(self, text):
        out = []
        for ch in text:
            cp = ord(ch)
            if cp == 0 or cp == 0xFFFD or (unicodedata.category(ch).startswith("C") and ch not in "\t\n\r"):
                continue
            if ch in "\t\n\r" or unicodedata.category(ch) == "Zs":
                out.append(" ")
            elif _is_cjk(cp):
                out.append(f" {ch} ")
            else:
                out.append(ch)
        tokens = []
        for word in "".join(out).lower().split():
            word = "".join(c for c in unicodedata.normalize("NFD", word) if unicodedata.category(c) != "Mn")
            cur = ""
            for ch in word:
                if _is_punct(ch):
                    if cur:
                        tokens.append(cur)
                    tokens.append(ch)
                    cur = ""
                else:
                    cur += ch
            if cur:
                tokens.append(cur)
        return tokens

    def _wordpiece(self, word):
        if len(word) > 100:
            return [self.unk]
        ids, start = [], 0
        while start < len(word):
            end, cur = len(word), None
            while start < end:
                piece = word[start:end] if start == 0 else "##" + word[start:end]
                if piece in self.vocab:
                    cur = self.vocab[piece]
                    break
                end -= 1
            if cur is None:
                return [self.unk]
            ids.append(cur)
            start = end
        return ids

    def encode(self, text, max_len=CONTEXT):
        ids = [i for w in self._basic(text) for i in self._wordpiece(w)]
        return [self.cls] + ids[:max_len - 2] + [self.sep]


class Clip:
    def __init__(self, model_dir=MODEL_DIR, threads=1):
        self.dir = Path(model_dir)
        self.threads = threads
        self._img = self._txt = self._tok = None
        self._lock = threading.Lock()
        self.cfg = None

    @property
    def available(self):
        return (self.dir / "image.xml").exists() and (self.dir / "text.xml").exists()

    def _compile(self, name):
        import openvino as ov
        core = ov.Core()
        return core.compile_model(str(self.dir / name), "CPU", {
            "INFERENCE_NUM_THREADS": self.threads, "PERFORMANCE_HINT": "LATENCY"})

    def _load_cfg(self):
        if self.cfg is None:
            self.cfg = json.loads((self.dir / "clip.json").read_text())

    def image(self, bgr):
        """Crop (BGR) -> unit 512-d float32 vector. Padded to a square (people are tall: a center
        crop would cut off head and feet), then resized to 224."""
        with self._lock:
            if self._img is None:
                self._load_cfg()
                self._img = self._compile("image.xml")
                log.info("CLIP image encoder loaded")
        size = self.cfg["image_size"]
        h, w = bgr.shape[:2]
        s = max(h, w)
        mean = np.array(self.cfg["mean"], dtype=np.float32)
        canvas = np.empty((s, s, 3), np.uint8)
        canvas[:] = (mean[::-1] * 255).astype(np.uint8)       # BGR mean grey: pads normalise to 0
        canvas[(s - h) // 2:(s - h) // 2 + h, (s - w) // 2:(s - w) // 2 + w] = bgr
        rgb = cv2.cvtColor(cv2.resize(canvas, (size, size), interpolation=cv2.INTER_CUBIC), cv2.COLOR_BGR2RGB)
        x = (rgb.astype(np.float32) / 255.0 - mean) / np.array(self.cfg["std"], dtype=np.float32)
        v = self._img([x.transpose(2, 0, 1)[None]])[0][0]
        return (v / max(float(np.linalg.norm(v)), 1e-9)).astype(np.float32)

    def text(self, s):
        with self._lock:
            if self._txt is None:
                self._load_cfg()
                self._tok = BertTokenizer(self.dir / "vocab.txt")
                self._txt = self._compile("text.xml")
                log.info("CLIP text encoder loaded")
        ids = np.array([self._tok.encode(s)], dtype=np.int64)
        v = self._txt([ids, np.ones_like(ids)])[0][0]
        return (v / max(float(np.linalg.norm(v)), 1e-9)).astype(np.float32)


clip = Clip()
