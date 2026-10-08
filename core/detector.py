"""
core/detector.py
Camera + face/emotion detection engine for the Live Monitor.

Runs a background thread that reads frames, detects faces via Haar cascade,
recognizes identities via LBPH, classifies emotions via a Keras model,
draws annotations, and pushes attendance events through the engine.
"""

import logging
import threading
import time

import cv2
import numpy as np

from core.camera_utils import CAM_BACKEND

logger = logging.getLogger(__name__)

# Shared lock protecting recognizer.predict() and recognizer.read()
# so hot-reload from the trainer cannot race with a prediction.
recognizer_lock = threading.Lock()


class Detector:
    """Camera capture + face/emotion detection loop.

    Parameters
    ----------
    cfg : dict
        Full application config.
    models : dict
        Must contain keys: ``cascade``, ``recognizer``, ``recognizer_ready``,
        ``emotion_model``, ``emotion_labels``.
    engine : AttendanceEngine
        Processes each detection for attendance logging.
    socketio : SocketIO
        Used to emit real-time events to connected clients.
    """

    def __init__(self, cfg, models, engine, socketio):
        self._cfg = cfg
        self._models = models  # mutable dict; recognizer can be hot-swapped
        self._engine = engine
        self._sio = socketio

        # Detection parameters (from config)
        self._frame_skip = cfg.get("frame_skip", 3)
        self._detect_scale = cfg.get("detect_scale", 0.5)
        self._threshold = cfg.get("confidence_threshold", 60)
        self._names = {int(k): v for k, v in cfg.get("names", {}).items()}

        # Runtime state
        self._cap = None
        self._thread = None
        self._stop_event = threading.Event()
        self.is_running = False
        self._cam_index = None

        # Thread-safe single-slot frame buffer (latest JPEG only)
        self._frame_lock = threading.Lock()
        self._jpeg_buffer = None

        # Live stats
        self._stats_lock = threading.Lock()
        self._fps = 0.0
        self._faces_count = 0
        self._known_count = 0

    # ── public controls ───────────────────────────────────────────────────

    def start(self, cam_index: int):
        """Open the camera and start the detection loop in a background thread."""
        if self.is_running:
            self.stop()

        self._stop_event.clear()
        self._cam_index = cam_index

        cap = cv2.VideoCapture(cam_index, CAM_BACKEND)
        if not cap.isOpened():
            cap.release()
            self.is_running = False
            self._cam_index = None
            logger.error("Failed to open camera index %d", cam_index)
            self._sio.emit("camera:error", {"message": f"Cannot open camera {cam_index}"})
            self._sio.emit("camera:status", self._camera_status_payload())
            return

        # Verify we can actually read a frame
        ret, frame = cap.read()
        if not ret or frame is None:
            cap.release()
            self.is_running = False
            self._cam_index = None
            logger.error("Camera %d opened but cannot read frames", cam_index)
            self._sio.emit("camera:error", {"message": f"Camera {cam_index} opened but returned no frames"})
            self._sio.emit("camera:status", self._camera_status_payload())
            return

        self._cap = cap
        self.is_running = True
        self._sio.emit("camera:status", self._camera_status_payload())

        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("Detector started on camera %d", cam_index)

    def stop(self):
        """Signal the loop to stop, wait for the thread, release the capture."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        if self._cap:
            self._cap.release()
            self._cap = None
        self.is_running = False
        self._jpeg_buffer = None
        with self._stats_lock:
            self._fps = 0.0
            self._faces_count = 0
            self._known_count = 0
        self._sio.emit("camera:status", self._camera_status_payload())
        logger.info("Detector stopped")

    def generate_frames(self):
        """Generator yielding multipart JPEG chunks for the MJPEG stream."""
        while True:
            with self._frame_lock:
                buf = self._jpeg_buffer
            if buf is not None:
                yield (b"--frame\r\n"
                       b"Content-Type: image/jpeg\r\n\r\n" + buf + b"\r\n")
            else:
                # No frame yet; yield a tiny delay to avoid busy-spin
                time.sleep(0.03)

    def get_stats(self):
        """Return live detector stats: fps, faces, known."""
        with self._stats_lock:
            return {
                "fps": round(self._fps, 1),
                "faces": self._faces_count,
                "known": self._known_count,
            }

    def update_threshold(self, value):
        self._threshold = value

    def update_names(self, names):
        self._names = {int(k): v for k, v in names.items()}
        self._engine.set_names(names)

    def set_auto_attend(self, enabled):
        self._engine.set_auto_attend(enabled)

    def _camera_status_payload(self):
        return {
            "running": self.is_running,
            "cam_index": self._cam_index,
        }

    # ── detection loop (runs in background thread) ────────────────────────

    def _loop(self):
        frame_count = 0
        prev_boxes = []  # cached detection results
        prev_identities = []  # [(person_id, is_known, emotion, confidence, name_label), ...]
        fps_timer = time.time()
        fps_frame_count = 0

        cascade = self._models["cascade"]
        recognizer = self._models["recognizer"]
        emotion_model = self._models["emotion_model"]
        emotion_labels = self._models["emotion_labels"]

        while not self._stop_event.is_set():
            ret, frame = self._cap.read()
            if not ret or frame is None:
                time.sleep(0.01)
                continue

            frame_count += 1
            fps_frame_count += 1

            # FPS calculation (every second)
            now = time.time()
            elapsed = now - fps_timer
            if elapsed >= 1.0:
                with self._stats_lock:
                    self._fps = fps_frame_count / elapsed
                fps_frame_count = 0
                fps_timer = now

            # ── detection on every Nth frame ──────────────────────────
            if frame_count % self._frame_skip == 0:
                gray_full = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                h, w = frame.shape[:2]
                small_w = int(w * self._detect_scale)
                small_h = int(h * self._detect_scale)
                small_gray = cv2.resize(gray_full, (small_w, small_h))

                faces_small = cascade.detectMultiScale(
                    small_gray, scaleFactor=1.3, minNeighbors=5, minSize=(30, 30)
                )

                # Scale boxes back up
                scale_inv = 1.0 / self._detect_scale
                boxes = []
                identities = []

                recognizer_ready = self._models.get("recognizer_ready", False)

                for (sx, sy, sw, sh) in faces_small if len(faces_small) > 0 else []:
                    x = int(sx * scale_inv)
                    y = int(sy * scale_inv)
                    bw = int(sw * scale_inv)
                    bh = int(sh * scale_inv)

                    # Clamp to frame bounds
                    x = max(0, x)
                    y = max(0, y)
                    bw = min(bw, w - x)
                    bh = min(bh, h - y)

                    boxes.append((x, y, bw, bh))

                    # ── identity ──────────────────────────────────────
                    face_roi = gray_full[y:y+bh, x:x+bw]
                    if face_roi.size == 0:
                        identities.append((-1, False, "Unknown", 0, "Unknown", None))
                        continue

                    if recognizer_ready:
                        try:
                            with recognizer_lock:
                                person_id, confidence = recognizer.predict(face_roi)
                            # Lower confidence = better match in LBPH
                            is_known = confidence < self._threshold
                        except Exception:
                            person_id, confidence, is_known = -1, 0, False
                    else:
                        person_id, confidence, is_known = -1, 0, False

                    # ── emotion ───────────────────────────────────────
                    try:
                        face_48 = cv2.resize(face_roi, (48, 48))
                        face_norm = face_48.astype("float32") / 255.0
                        face_input = np.expand_dims(np.expand_dims(face_norm, axis=-1), axis=0)
                        preds = emotion_model.predict(face_input, verbose=0)
                        emotion_idx = int(np.argmax(preds[0]))
                        emotion = emotion_labels.get(emotion_idx, "Unknown")
                    except Exception:
                        emotion = "Unknown"

                    name_label = self._names.get(person_id, "Unknown") if is_known else "Unknown"
                    identities.append((person_id, is_known, emotion, confidence, name_label, face_roi))

                prev_boxes = boxes
                prev_identities = identities

            # ── draw + attendance ─────────────────────────────────────
            faces_this_frame = 0
            known_this_frame = 0

            for i, (x, y, bw, bh) in enumerate(prev_boxes):
                if i >= len(prev_identities):
                    break
                person_id, is_known, emotion, confidence, name_label, face_roi = prev_identities[i]

                # Box color: green for known, red for unknown
                color = (0, 255, 0) if is_known else (0, 0, 255)
                cv2.rectangle(frame, (x, y), (x + bw, y + bh), color, 2)

                # Label
                label = f"{name_label} - {emotion}"
                cv2.putText(frame, label, (x, y - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)

                faces_this_frame += 1
                if is_known:
                    known_this_frame += 1

                # ── attendance event (only on detection frames) ───────
                if frame_count % self._frame_skip == 0:
                    record = self._engine.process_detection(
                        person_id, is_known, emotion, float(confidence), face_crop=face_roi
                    )
                    if record is not None:
                        # Clean for JSON serialization
                        clean = {k: v for k, v in record.items() if k != "_id"}
                        for key, val in clean.items():
                            if hasattr(val, "isoformat"):
                                clean[key] = val.isoformat()
                        self._sio.emit("attendance:new", clean)
                        stats = self._engine.get_today_stats()
                        self._sio.emit("stats:update", stats)

            with self._stats_lock:
                self._faces_count = faces_this_frame
                self._known_count = known_this_frame

            # ── encode to JPEG and store in single-slot buffer ────────
            ret_enc, jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ret_enc:
                with self._frame_lock:
                    self._jpeg_buffer = jpeg.tobytes()

            # Small sleep to cap CPU usage
            time.sleep(0.005)
