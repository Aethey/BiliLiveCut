#!/usr/bin/env python3
"""Explicit local-model ASR integration check; never downloads models or calls an LLM."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    """Validate installed models, then run the production isolated ASR pipeline."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", required=True, type=Path, help="Existing speech or silence recording")
    parser.add_argument(
        "--models-dir", required=True, type=Path, help="Verified installed Engine Pack models directory"
    )
    parser.add_argument("--engine", choices=("whisper", "paraformer", "funasr_nano"), default="whisper")
    parser.add_argument("--device", choices=("cpu", "cuda"), default="cpu")
    parser.add_argument("--expect", choices=("speech", "no_speech"), default="speech")
    parser.add_argument("--contains", default="", help="Optional expected text fragment; not included in the report")
    parser.add_argument("--output", type=Path, help="Optional JSON report (does not include transcript text)")
    args = parser.parse_args(argv)
    audio = args.audio.resolve()
    models = args.models_dir.resolve()
    output = args.output.resolve() if args.output else None
    if not audio.is_file() or audio.stat().st_size == 0:
        parser.error("--audio must be an existing nonempty local file")
    if not (models / "engine-pack-installed.json").is_file():
        parser.error("--models-dir must contain engine-pack-installed.json; prepare the Engine Pack first")
    if args.expect == "no_speech" and args.contains:
        parser.error("--contains cannot be used with --expect no_speech")

    sys.path.insert(0, str(REPO_ROOT))
    sys.path.insert(0, str(REPO_ROOT / "packaging/portable/src"))
    from blc_portable.engine_pack.installer import check_installed_models

    valid, errors = check_installed_models(models, full_rehash=True)
    if not valid:
        raise RuntimeError("Installed model verification failed: " + "; ".join(errors))

    # Fresh CLI process only: a temporary cwd and explicit environment prevent reading
    # the repository .env or writing to the user's database, media or model caches.
    original_cwd = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="blc-local-asr-") as temporary:
        os.chdir(temporary)
        os.environ.update(
            DATABASE_URL="sqlite:///:memory:",
            STORAGE_ROOT=str(Path(temporary) / "storage"),
            PLUGIN_DIR=str(Path(temporary) / "plugins"),
            BLC_MODELS_DIR=str(models),
            HF_HUB_OFFLINE="1",
            TRANSFORMERS_OFFLINE="1",
            MODELSCOPE_CACHE=str(Path(temporary) / "modelscope"),
            HF_HOME=str(Path(temporary) / "huggingface"),
            ASR_PRIMARY=args.engine,
            ASR_PRIMARY_DEVICE=args.device,
            ASR_AUXILIARY_DEVICE=args.device,
            ASR_FALLBACK_DEVICE=args.device,
            ASR_REVIEW_DEVICE=args.device,
            WHISPER_DEVICE=args.device,
            WHISPER_COMPUTE_TYPE="int8",
            ASR_PRELOAD_ON_START="false",
        )
        try:
            from app.analysis.transcription.isolated import IsolatedPipeline
            from app.analysis.transcription.process_pool import ASRProcessPool

            pool = ASRProcessPool()
            started = time.monotonic()
            try:
                result = IsolatedPipeline(pool=pool, context={"operation": "local_asr_check"}).transcribe(str(audio))
            finally:
                pool.shutdown(0)
            text = (result.final_text or result.text).strip()
            if result.result_type != args.expect or bool(text) != (args.expect == "speech"):
                raise AssertionError(f"Expected {args.expect}, got {result.result_type}; text_chars={len(text)}")
            if args.contains and args.contains not in text:
                raise AssertionError("ASR text does not contain the requested fragment")
            if any(not (0 <= segment.start <= segment.end) for segment in result.segments):
                raise AssertionError("ASR segment timestamps are invalid")
            report = {
                "engine": args.engine,
                "device": args.device,
                "backend": result.backend,
                "result_type": result.result_type,
                "text_chars": len(text),
                "segments": len(result.segments),
                "elapsed_s": round(time.monotonic() - started, 3),
                "audio_duration_s": result.audio_duration,
                "models_full_rehash": True,
            }
        finally:
            os.chdir(original_cwd)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
