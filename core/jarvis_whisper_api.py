#!/usr/bin/env python3
"""JARVIS Whisper API — OpenAI-compatible /v1/audio/transcriptions using faster-whisper"""

from flask import Flask, request, jsonify
import tempfile
import os
import time

app = Flask("jarvis-whisper")

# Lazy-load model (base.en par défaut — rapide, ~150MB)
_model = None
_model_size = os.environ.get("WHISPER_MODEL", "base")


def get_model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel

        print(f"[Whisper] Loading model: {_model_size}...")
        _model = WhisperModel(_model_size, device="cpu", compute_type="int8")
        print("[Whisper] Model ready")
    return _model


@app.route("/health")
def health():
    return jsonify(
        {"status": "ok", "model": _model_size, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}
    )


@app.route("/v1/audio/transcriptions", methods=["POST"])
def transcribe():
    """OpenAI-compatible audio transcription endpoint."""
    if "file" not in request.files:
        return jsonify({"error": "no file provided"}), 400

    audio_file = request.files["file"]
    language = request.form.get("language", None)
    response_format = request.form.get("response_format", "json")

    # Save to temp file
    suffix = os.path.splitext(audio_file.filename or "audio.wav")[1] or ".wav"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        audio_file.save(tmp.name)
        tmp_path = tmp.name

    try:
        model = get_model()
        t0 = time.perf_counter()
        segments, info = model.transcribe(
            tmp_path,
            language=language,
            beam_size=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 300},
        )
        text = " ".join(seg.text.strip() for seg in segments)
        duration = round((time.perf_counter() - t0) * 1000)

        if response_format == "text":
            return text, 200, {"Content-Type": "text/plain"}
        elif response_format == "verbose_json":
            return jsonify(
                {
                    "text": text,
                    "language": info.language,
                    "duration": info.duration,
                    "latency_ms": duration,
                }
            )
        else:
            return jsonify({"text": text})

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        os.unlink(tmp_path)


@app.route("/v1/models")
def models():
    return jsonify({"data": [{"id": f"whisper-{_model_size}", "object": "model"}]})


if __name__ == "__main__":
    print(f"[JARVIS Whisper API] Starting on :9743 (model={_model_size})")
    print("Endpoint: POST http://localhost:9743/v1/audio/transcriptions")
    # Pre-load model at startup
    get_model()
    app.run(host="0.0.0.0", port=9743, debug=False, threaded=True)
