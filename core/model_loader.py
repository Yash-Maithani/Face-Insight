"""
core/model_loader.py
Loads config.json, Haar cascade, LBPH recognizer, emotion model,
and builds name/emotion lookup dicts.
"""

import json
import os
import sys
import logging

import cv2

logger = logging.getLogger(__name__)

# ─── Default config ──────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "camera": 0,
    "frame_skip": 3,
    "detect_scale": 0.5,
    "confidence_threshold": 60,
    "trainer_file": "Trainer.yml",
    "emotion_model": "models/emotion_model.h5",
    "emotions": ["Angry", "Disgusted", "Fearful", "Happy", "Neutral", "Sad", "Surprised"],
    "data_folder": "ImageData",
    "max_images": 500,
    "screenshot_folder": "screenshots",
    "names": {},
    "mongodb_uri": "mongodb://localhost:27017",
    "mongodb_db": "face_attendance",
    "firebase_url": "",
    "firebase_credentials": "",
    "attendance": {
        "cooldown_minutes": 60,
        "late_after": "09:30",
        "working_hours_end": "17:30",
        "auto_attend": True,
    },
}

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")


# ─── Config helpers ───────────────────────────────────────────────────────────

def load_config(path: str = None) -> dict:
    """Load config.json, creating it with the defaults if missing."""
    if path is None:
        path = os.environ.get("FACE_WEB_CONFIG", CONFIG_PATH)
    if not os.path.exists(path):
        logger.info("config.json not found – creating with defaults at %s", path)
        save_config(DEFAULT_CONFIG, path)
        return dict(DEFAULT_CONFIG)  # shallow copy of top-level
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg


def save_config(cfg: dict, path: str = None) -> None:
    """Persist config dict to disk."""
    if path is None:
        path = os.environ.get("FACE_WEB_CONFIG", CONFIG_PATH)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# ─── Model / data loaders ────────────────────────────────────────────────────

def load_haar_cascade():
    """Load Haar frontal-face cascade.

    Checks cv2.data.haarcascades first (OpenCV <= 4.x ships it there).
    Falls back to the copy in models/ (needed for OpenCV 5.x+, which dropped
    the bundled XMLs).
    """
    cascade_filename = "haarcascade_frontalface_default.xml"
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # Candidate paths: OpenCV data dir first, then our own copy
    candidates = [
        os.path.join(cv2.data.haarcascades, cascade_filename),
        os.path.join(project_root, "models", cascade_filename),
    ]

    for path in candidates:
        if os.path.exists(path):
            cascade = cv2.CascadeClassifier(path)
            if not cascade.empty():
                logger.info("Haar cascade loaded from %s", path)
                return cascade

    raise RuntimeError(
        f"Failed to load Haar cascade. Searched: {candidates}. "
        "Download it from https://github.com/opencv/opencv/blob/4.x/data/haarcascades/"
        "haarcascade_frontalface_default.xml and place it in the models/ folder."
    )


def load_recognizer(trainer_path: str):
    """
    Load the LBPH face recognizer.

    Returns (recognizer, recognizer_ready).
    If Trainer.yml is missing the recognizer is returned *uninitialized*
    and recognizer_ready is False – every face will be treated as Unknown
    until the first successful training run.
    """
    recognizer = cv2.face.LBPHFaceRecognizer_create()
    if os.path.exists(trainer_path):
        recognizer.read(trainer_path)
        logger.info("LBPH recognizer loaded from %s", trainer_path)
        return recognizer, True
    else:
        logger.warning("Trainer file '%s' not found – recognizer uninitialized; all faces will be Unknown.", trainer_path)
        return recognizer, False


def load_emotion_model(emotion_model_path: str):
    """
    Load the pre-trained Keras emotion model.

    This IS a hard requirement – a missing file causes a startup failure
    with an explicit error message naming the absent path.
    """
    if not os.path.exists(emotion_model_path):
        msg = (
            f"FATAL: emotion model file not found at '{os.path.abspath(emotion_model_path)}'. "
            "It ships in models/. Check the emotion_model path in config.json."
        )
        logger.error(msg)
        raise FileNotFoundError(msg)

    # Delay TensorFlow import so tests that don't need it stay fast
    import tensorflow as tf  # noqa: E402
    model = tf.keras.models.load_model(emotion_model_path)
    logger.info("Emotion model loaded from %s", emotion_model_path)
    return model


def build_emotion_labels(cfg: dict) -> dict:
    """Build {index: label} map from cfg['emotions'] list."""
    return {i: label for i, label in enumerate(cfg["emotions"])}


def build_names(cfg: dict) -> dict:
    """Build {int_id: name} map from cfg['names'] (keys may be strings)."""
    return {int(k): v for k, v in cfg["names"].items()}
