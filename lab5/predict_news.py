"""CPU-класифікація. Запуск з lab5: python predict_news.py "Заголовок"."""

import argparse
import json
import os
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort
import psutil
from tokenizers import Tokenizer


def main():
    """Завантажує ONNX-модель і класифікує аргументи або рядки зі stdin."""
    parser = argparse.ArgumentParser()
    parser.add_argument("titles", nargs="*")
    parser.add_argument("--model-dir", type=Path, default=Path(__file__).parent / "artifacts" / "minilm_cpu")
    parser.add_argument("--input-file", type=Path)
    args = parser.parse_args()
    config = json.loads((args.model_dir / "inference.json").read_text(encoding="utf-8"))
    tokenizer = Tokenizer.from_file(str(args.model_dir / "tokenizer.json"))
    tokenizer.enable_truncation(max_length=config["max_length"])
    options = ort.SessionOptions()
    # Один потік відповідає серверу з одним доступним ядром.
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(str(args.model_dir / "model.int8.onnx"), options, providers=["CPUExecutionProvider"])
    required = {item.name for item in session.get_inputs()}

    def classify(title):
        """Повертає ймовірність класу «показати» та рішення за збереженим порогом."""
        encoded = tokenizer.encode(title)
        inputs = {"input_ids": np.array([encoded.ids], dtype=np.int64),
                  "attention_mask": np.array([encoded.attention_mask], dtype=np.int64)}
        logits = session.run(None, {key: value for key, value in inputs.items() if key in required})[0][0]
        probabilities = np.exp(logits - logits.max())
        probability = float(probabilities[1] / probabilities.sum())
        return {"text": title, "probability": probability, "show": probability >= config["threshold"]}

    if args.input_file:
        titles = json.loads(args.input_file.read_text(encoding="utf-8"))
        classify(titles[0])  # Прогрів виключено з часу обробки пачки.
        start = time.perf_counter()
        results = [classify(title) for title in titles]
        info = psutil.Process(os.getpid()).memory_info()
        print(json.dumps({"results": results, "elapsed_s": time.perf_counter() - start,
                          "rss_mb": info.rss / 1e6, "peak_rss_mb": getattr(info, "peak_wset", info.rss) / 1e6}))
        return
    if args.titles:
        for title in args.titles:
            print(json.dumps(classify(title), ensure_ascii=False))
        return
    # Процес залишається завантаженим і приймає нові заголовки по одному.
    import sys
    for line in sys.stdin:
        if line.strip():
            print(json.dumps(classify(line.strip()), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
