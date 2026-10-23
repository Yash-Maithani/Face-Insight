"""
core/trainer.py
Trains the LBPH face recognizer from flat images in ImageData/.

Image naming convention: <person_id>_<n>.<ext>
e.g. ImageData/3_17.jpg

The person_id is parsed from the prefix before the first underscore.
Subfolders and unparseable filenames are silently skipped; their count
is reported in bad_skipped inside the train:done payload.

Runs in a background thread.  On success, calls read() on the SAME
recognizer object the detector already holds (inside recognizer_lock)
and sets recognizer_ready = True.

On ANY failure (zero images, cv2 error, …):
  - emits train:error with the message
  - emits train:status  {"running": False, …}
  - still calls on_done() so a paused Live Monitor can resume
  - never propagates an exception out of the thread
"""

import logging
import os
import threading

import cv2
import numpy as np

logger = logging.getLogger(__name__)


class ModelTrainer:
    """LBPH face recognizer trainer.

    Parameters
    ----------
    cfg : dict
        Full app config (needs ``data_folder``, ``trainer_file``).
    models : dict
        The shared mutable models dict.  On successful training,
        ``models["recognizer"].read(trainer_path)`` is called *in place*
        and ``models["recognizer_ready"]`` is set to True.
    recognizer_lock : threading.Lock
        Shared lock with the detector, held while calling
        recognizer.read() so predict() cannot race with it.
    socketio : SocketIO
        For emitting training progress events.
    """

    def __init__(self, cfg, models, recognizer_lock, socketio):
        self._cfg   = cfg
        self._models = models
        self._lock  = recognizer_lock
        self._sio   = socketio

        self._data_folder  = cfg.get("data_folder",  "ImageData")
        self._trainer_file = cfg.get("trainer_file", "Trainer.yml")

        # Runtime state
        self._thread   = None
        self.is_active = False
        self._pct      = 0
        self._msg      = ""

    # ---- public interface -------------------------------------------------

    def start(self, on_done=None):
        """Start training in a background thread.

        Parameters
        ----------
        on_done : callable, optional
            Called (no args) after training finishes, whether it
            succeeded or failed.  Used by server.py to resume the LM.

        Returns (True, None) if started, (False, error_string) if busy.
        """
        if self.is_active:
            return False, "Training already in progress"

        self.is_active = True
        self._pct      = 0
        self._msg      = "Starting..."
        self._sio.emit("train:status", self.get_status())

        self._thread = threading.Thread(
            target=self._run, args=(on_done,), daemon=True
        )
        self._thread.start()
        return True, None

    def get_status(self):
        """Return JSON-serializable status dict.

        Keys
        ----
        active   : bool  (legacy key kept for compatibility)
        running  : bool  (same value)
        pct      : int   progress 0-100
        msg      : str   human-readable message
        """
        return {
            "active":  self.is_active,
            "running": self.is_active,
            "pct":     self._pct,
            "msg":     self._msg,
        }

    # ---- internal ---------------------------------------------------------

    def _run(self, on_done=None):
        """Top-level thread target: never raises."""
        try:
            self._do_train()
        except Exception as exc:
            logger.error("Training failed: %s", exc)
            self._msg = str(exc)
            self._sio.emit("train:error", {"message": str(exc)})
        finally:
            self.is_active = False
            self._pct      = 0
            self._sio.emit("train:status", self.get_status())
            if on_done:
                try:
                    on_done()
                except Exception as exc:
                    logger.error("train on_done callback failed: %s", exc)

    def _do_train(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        data_path = (
            self._data_folder
            if os.path.isabs(self._data_folder)
            else os.path.join(project_root, self._data_folder)
        )
        trainer_path = (
            self._trainer_file
            if os.path.isabs(self._trainer_file)
            else os.path.join(project_root, self._trainer_file)
        )

        if not os.path.isdir(data_path):
            raise RuntimeError(f"Data folder not found: {data_path}")

        # ── Scan flat files ──────────────────────────────────────────────
        faces        = []
        ids          = []
        bad_skipped  = 0
        person_stats = {}   # {person_id: count}

        all_entries = sorted(os.listdir(data_path))
        image_files = [
            f for f in all_entries
            if not os.path.isdir(os.path.join(data_path, f))
            and f.lower().endswith((".jpg", ".jpeg", ".png"))
        ]
        total_files = len(image_files)

        self._msg = f"Scanning {total_files} image(s)..."
        self._sio.emit("train:progress", {
            "step": "scanning", "total": total_files,
        })

        for file_idx, fname in enumerate(image_files):
            # Parse person_id from prefix before the first underscore
            stem = os.path.splitext(fname)[0]
            if "_" not in stem:
                bad_skipped += 1
                continue
            pid_str = stem.split("_", 1)[0]
            try:
                person_id = int(pid_str)
            except ValueError:
                bad_skipped += 1
                continue

            img_path = os.path.join(data_path, fname)
            img = cv2.imread(img_path, cv2.IMREAD_GRAYSCALE)
            if img is None:
                bad_skipped += 1
                continue

            faces.append(img)
            ids.append(person_id)
            person_stats[person_id] = person_stats.get(person_id, 0) + 1

            pct = int((file_idx + 1) / total_files * 70) if total_files else 0
            self._pct = pct
            self._sio.emit("train:progress", {
                "step":      "loading",
                "current":   file_idx + 1,
                "total":     total_files,
                "person_id": person_id,
            })

        if not faces:
            raise RuntimeError(
                f"No usable face images found in {data_path} "
                f"({bad_skipped} file(s) skipped due to bad names/unreadable)."
            )

        # ── Train ────────────────────────────────────────────────────────
        self._pct = 70
        self._msg = f"Training on {len(faces)} image(s)…"
        self._sio.emit("train:progress", {
            "step": "training", "total_images": len(faces),
        })

        tmp_recognizer = cv2.face.LBPHFaceRecognizer_create()
        tmp_recognizer.train(faces, np.array(ids))

        # ── Save ─────────────────────────────────────────────────────────
        self._pct = 90
        tmp_recognizer.save(trainer_path)
        logger.info("Trainer.yml written to %s (%d images, %d persons)",
                    trainer_path, len(faces), len(person_stats))

        # ── Hot-reload IN PLACE ──────────────────────────────────────────
        # Call read() on the SAME object so anyone holding a reference
        # (e.g. the detector's local variable) sees the update.
        with self._lock:
            self._models["recognizer"].read(trainer_path)
            self._models["recognizer_ready"] = True

        self._pct = 100
        self._msg = "Done"

        # Build per-person list for train:done
        persons_list = [
            {"name": str(pid), "count": cnt}
            for pid, cnt in sorted(person_stats.items())
        ]

        self._sio.emit("train:done", {
            "persons":      persons_list,
            "total_images": len(faces),
            "bad_skipped":  bad_skipped,
        })
        logger.info("Recognizer hot-reloaded in place (%d persons)", len(person_stats))
