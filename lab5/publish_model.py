"""Prepare a trained SetFit model for Hugging Face. Upload requires --publish."""

import argparse
import json
import shutil
from pathlib import Path

from huggingface_hub import HfApi
from huggingface_hub.utils import validate_repo_id

ROOT = Path(__file__).resolve().parent


def prepare(repo_id, variant):
    """Copy model files and write a model card with the measured results."""
    source = ROOT / "artifacts" / f"minilm_{variant}" / "model"
    if not (source / "config_setfit.json").is_file():
        raise FileNotFoundError(f"Train the notebook first. Model not found at {source}")
    result = json.loads((ROOT / "results" / "minilm" / f"{variant}.json").read_text(encoding="utf-8"))
    destination = ROOT / "artifacts" / "hub-export" / variant
    shutil.copytree(source, destination, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(".git", ".cache", "__pycache__", "*.log"))
    metrics = result["common_test"]
    card = f"""---
language:
- uk
- ru
license: apache-2.0
library_name: setfit
pipeline_tag: text-classification
base_model: sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
tags:
- setfit
- news
- text-classification
- personal-preferences
---

# Personal news headline filter

Fine-tuned multilingual MiniLM with SetFit and a logistic regression head.
Variant: **{variant}**. Class 0 means skip, class 1 means show.

## Training

Body learning rate 2e-5, batch size 16, 80 steps, maximum length 96.
Repeated AI annotations of the same URL are consolidated by majority vote.
Ties use the most recent annotation. Training includes {result['train_unique']}
distinct URLs. Model files include the complete encoder and classification head.

## Evaluation

The shared human-labeled test contains {metrics['n']} headlines.
Macro-F1: {metrics['macro_f1']:.3f}.
Show precision: {metrics['show_precision']:.3f}.
Show recall: {metrics['show_recall']:.3f}.
Before body fine-tuning, Macro-F1 was {result['before']['macro_f1']:.3f}.

This is a small personal dataset and the test has been reused for model
comparisons. Some teacher prompts had previously included human-rated examples.
The classifier sees titles only, including some cases whose labels depend on
previously read news. These results do not establish general news relevance.

## Usage

```python
from setfit import SetFitModel

model = SetFitModel.from_pretrained("{repo_id}", device="cpu")
titles = ["A new science museum opened in the city"]
probability = model.predict_proba(titles, as_numpy=True)[:, 1]
show = probability >= {result['threshold']}
print(show.tolist())
```

Use the recorded threshold {result['threshold']}, selected on validation.
It can differ from the model's default 0.5 classification threshold.

## Files and license

This repository contains the complete fine-tuned SetFit model, including
the sklearn head. It is a full model artifact.
The base model uses Apache-2.0. Training headlines and personal ratings
are excluded from this upload. No separate redistribution license for the
source news headlines was specified in the provided dataset.
"""
    (destination / "README.md").write_text(card, encoding="utf-8")
    return destination


def main():
    """Prepare locally, then publish only if the command explicitly requests it."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-id", required=True, help="Your Hugging Face username/model-name")
    parser.add_argument("--variant", choices=["ai", "human", "mixed"], default="mixed")
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    validate_repo_id(args.repo_id)
    destination = prepare(args.repo_id, args.variant)
    files = [path for path in destination.rglob("*") if path.is_file()]
    print("Prepared", destination)
    print("Files", len(files), "MB", round(sum(path.stat().st_size for path in files) / 1e6, 1))
    if args.publish:
        api = HfApi()
        api.whoami()
        api.create_repo(repo_id=args.repo_id, repo_type="model", private=False, exist_ok=True)
        api.upload_folder(repo_id=args.repo_id, repo_type="model", folder_path=str(destination),
                          commit_message="Upload fine-tuned MiniLM SetFit news classifier")
        print(f"https://huggingface.co/{args.repo_id}")


if __name__ == "__main__":
    main()
