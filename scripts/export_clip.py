"""Export Chinese-CLIP (ViT-B/16) to OpenVINO for the AI 查询 feature.

    .venv-export/bin/python scripts/export_clip.py      # -> worker/models/chinese-clip-vit-b16/

Two models: image.xml (person / vehicle crop -> 512-d vector, run by the API for every sighting)
and text.xml (Chinese description -> 512-d vector, run per search). Both are dynamic-shape and
weight-compressed to INT8 (NNCF compress_weights: about a quarter of the memory, vectors almost
unchanged; the check at the end prints the cosine to the FP32 model). vocab.txt + clip.json are
what api/clip.py needs to tokenize and preprocess without transformers / torch at runtime.
Runs in .venv-export (needs torch, transformers, nncf). Set HF_ENDPOINT=https://hf-mirror.com if
huggingface.co is unreachable.
"""
import json
import shutil
import sys
from pathlib import Path

import nncf
import numpy as np
import openvino as ov
import torch
from transformers import ChineseCLIPModel, ChineseCLIPProcessor

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from api.clip import BertTokenizer, CONTEXT  # noqa: E402  (same tokenizer as at runtime)

HF_ID = "OFA-Sys/chinese-clip-vit-base-patch16"
OUT = ROOT / "worker" / "models" / "chinese-clip-vit-b16"


def tensor(x, field):
    """transformers 4 returns the projected features; 5 a BaseModelOutputWithPooling whose
    pooler_output holds them (last_hidden_state is per token)."""
    return x if isinstance(x, torch.Tensor) else x.pooler_output


class Image(torch.nn.Module):
    def __init__(self, m):
        super().__init__()
        self.m = m

    def forward(self, pixel_values):
        return tensor(self.m.get_image_features(pixel_values=pixel_values), "image_embeds")


class Text(torch.nn.Module):
    def __init__(self, m):
        super().__init__()
        self.m = m

    def forward(self, input_ids, attention_mask):
        return tensor(self.m.get_text_features(input_ids=input_ids, attention_mask=attention_mask,
                                               token_type_ids=torch.zeros_like(input_ids)), "text_embeds")


def main():
    model = ChineseCLIPModel.from_pretrained(HF_ID).eval()
    proc = ChineseCLIPProcessor.from_pretrained(HF_ID)
    OUT.mkdir(parents=True, exist_ok=True)
    img = Image(model)
    txt = Text(model)
    ids = torch.ones(1, 8, dtype=torch.long)
    with torch.no_grad():
        ov_img = ov.convert_model(img, example_input=torch.zeros(1, 3, 224, 224),
                                  input=[("pixel_values", [-1, 3, 224, 224])])
        ov_txt = ov.convert_model(txt, example_input=(ids, ids),
                                  input=[("input_ids", [-1, -1], ov.Type.i64), ("attention_mask", [-1, -1], ov.Type.i64)])
    ov_img_q = nncf.compress_weights(ov_img)
    ov_txt_q = nncf.compress_weights(ov_txt)
    ov.save_model(ov_img_q, str(OUT / "image.xml"))
    ov.save_model(ov_txt_q, str(OUT / "text.xml"))
    vocab = Path(proc.tokenizer.vocab_file) if getattr(proc.tokenizer, "vocab_file", None) else None
    if vocab is None or not vocab.exists():
        proc.tokenizer.save_vocabulary(str(OUT))
    else:
        shutil.copy(vocab, OUT / "vocab.txt")
    ip = proc.image_processor
    json.dump({"hf_id": HF_ID, "image_size": 224, "mean": list(ip.image_mean), "std": list(ip.image_std),
               "context": CONTEXT}, open(OUT / "clip.json", "w"), indent=1)

    # check: our tokenizer == transformers', compressed model ~= torch model
    tok = BertTokenizer(OUT / "vocab.txt")
    core = ov.Core()
    ci, ct = core.compile_model(OUT / "image.xml", "CPU"), core.compile_model(OUT / "text.xml", "CPU")
    for s in ["超市里穿绿色外套的顾客", "穿橙色反光背心、戴安全帽的人", "a man in a Red T-shirt, 2 bikes"]:
        ref = proc.tokenizer(s, max_length=CONTEXT, truncation=True)["input_ids"]
        mine = tok.encode(s)
        assert mine == ref, (s, mine, ref)
        with torch.no_grad():
            a = txt(torch.tensor([ref]), torch.ones(1, len(ref), dtype=torch.long)).numpy()[0]
        b = ct([np.array([mine]), np.ones((1, len(mine)), dtype=np.int64)])[0][0]
        print(f"text  cos {np.dot(a, b) / np.linalg.norm(a) / np.linalg.norm(b):.4f}  {s}")
    x = torch.randn(1, 3, 224, 224)
    with torch.no_grad():
        a = img(x).numpy()[0]
    b = ci([x.numpy()])[0][0]
    print(f"image cos {np.dot(a, b) / np.linalg.norm(a) / np.linalg.norm(b):.4f}")
    mb = sum(p.stat().st_size for p in OUT.glob("*.bin")) / 1e6
    print(f"{OUT}: {mb:.0f} MB")


if __name__ == "__main__":
    main()
