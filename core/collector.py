"""
core/collector.py
Face-image data collector for the Register tab.

Opens the camera, detects faces via the Haar cascade from model_loader,
crop-augments each one, and writes flat to:
    <data_folder>/<person_id>_<count>.jpg
e.g. ImageData/3_17.jpg

Numbering continues from the highest existing number for that person_id.
Exposes a live MJPEG preview and emits Socket.IO progress events.
"""

import logging
import os
import threading
import time

import cv2
import numpy as np

from core.camera_utils import CAM_BACKEND

logger = logging.getLogger(__name__)


def augment_crop(gray_face):
    """Return a list of augmented grayscale face crops.

    Augmentations:
      1. original
      2. horizontal flip
      3. brightness +30
      4. brightness -30

    All returned images have the same shape as the input.
    """
    variants = [gray_face]
    # Horizontal flip
    variants.append(cv2.flip(gray_face, 1))
    # Brightness adjustments (clamped to [0, 255] by cv2.add / cv2.subtract)
    bright = cv2.add(gray_face, np.full_like(gray_face, 30))
    dark   = cv2.subtract(gray_face, np.full_like(gray_face, 30))
    variants.append(bright)
    variants.append(dark)
    return variants


def _next_count(data_folder, person_id):
    """Return the next sequential image number for *person_id*.

    Scans the flat data_folder for files named ``<person_id>_<n>.jpg``
    and returns max(n) + 1, or 1 if no files exist yet.
    """
    prefix = f"{person_id}_"
    max_n = 0
    if os.path.isdir(data_folder):
        for fname in os.listdir(data_folder):
            if not fname.lower().endswith((".jpg", ".jpeg", ".png")):
                continue
            if fname.startswith(prefix):
                stem = fname[len(prefix):]
                stem = os.path.splitext(stem)[0]
                try:
                    n = int(stem)
                    if n > max_n:
                        max_n = n
                except ValueError:
                    pass
    return max_n + 1


class DataCollector:
    """Captures face images for training.

    Parameters
    ----------
    cfg : dict
        Full app config (needs ``data_folder``, ``max_images``).
    cascade : cv2.CascadeClassifier
        The shared Haar cascade loaded by model_loader.
    socketio : SocketIO
        For emitting collection progress events.
    """

    def __init__(self, cfg, cascade, socketio):
        self._cfg      = cfg
        self._cascade  = cascade
        self._sio      = socketio

        self._data_folder = cfg.get("data_folder", "ImageData")

        # Runtime state
        self._cap         = None
        self._thread      = None
        self._stop_event  = threading.Event()
        self.is_active    = False

        # Current session
        self._person_id   = None
        self._person_name = None
        self._max_images  = 0
        self._collected   = 0

        # Resume callback: set by server so the LM restarts after
        # collection ends (done, early stop, or camera error).
        self._on_done     = None

        # MJPEG preview buffer
        self._frame_lock  = threading.Lock()
        self._jpeg_buffer = None

    # ---- public controls --------------------------------------------------

    def start(self, person_id, name, max_images, cam_index=0, on_done=None):
        """Begin collecting face images.

        Parameters
        ----------
        on_done : callable, optional
            Called (no args) after the collection session ends for any
            reason (success, early stop, camera error).  Used by the
            server to resume the Live Monitor.

        Returns (True, None) on success, (False, error_string) on failure.
        """
        if self.is_active:
            return False, "Collection already in progress"

        self._person_id   = int(person_id)
        self._person_name = name
        self._max_images  = int(max_images)
        self._collected   = 0
        self._on_done     = on_done
        self._stop_event.clear()

        # Ensure the flat data folder exists
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        data_path = (
            self._data_folder
            if os.path.isabs(self._data_folder)
            else os.path.join(project_root, self._data_folder)
        )
        os.makedirs(data_path, exist_ok=True)
        self._data_path = data_path

        # Continue numbering from the highest existing file for this person
        self._next_idx = _next_count(data_path, self._person_id)

        # Open camera
        cap = cv2.VideoCapture(cam_index, CAM_BACKEND)
        if not cap.isOpened():
            cap.release()
            err = f"Cannot open camera {cam_index}"
            logger.error(err)
            self._sio.emit("collect:error", {"message": err})
            self._sio.emit("collect:status", self.get_status())
            self._fire_on_done("error")
            return False, err

        ret, frame = cap.read()
        if not ret or frame is None:
            cap.release()
            err = f"Camera {cam_index} opened but returned no frames"
            logger.error(err)
            self._sio.emit("collect:error", {"message": err})
            self._sio.emit("collect:status", self.get_status())
            self._fire_on_done("error")
            return False, err

        self._cap      = cap
        self.is_active = True

        self._sio.emit("collect:status", self.get_status())

        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("DataCollector started for person %d (%s), max=%d",
                    self._person_id, self._person_name, self._max_images)
        return True, None

    def stop(self):
        """Stop the collection loop and release the camera."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        if self._cap:
            self._cap.release()
            self._cap = None
        self.is_active    = False
        self._jpeg_buffer = None
        self._sio.emit("collect:status", self.get_status())
        logger.info("DataCollector stopped (collected %d images)", self._collected)
        self._fire_on_done("stopped")

    def generate_frames(self):
        """Generator yielding multipart JPEG chunks for the collection MJPEG stream."""
        while True:
            with self._frame_lock:
                buf = self._jpeg_buffer
            if buf is not None:
                yield (b"--frame\r\n"
                       b"Content-Type: image/jpeg\r\n\r\n" + buf + b"\r\n")
            else:
                time.sleep(0.03)

    def get_status(self):
        """Return JSON-serializable status dict.

        Keys
        ----
        active       : bool  (also kept as ``running`` for the new payload)
        running      : bool  (same as active)
        count        : int   (images collected so far)
        target       : int   (max_images for this session)
        person_name  : str | None
        person_id    : int | None
        """
        return {
            "active":       self.is_active,
            "running":      self.is_active,
            "count":        self._collected,
            "target":       self._max_images,
            "person_name":  self._person_name,
            "person_id":    self._person_id,
        }

    # ---- internal helpers -------------------------------------------------

    def _fire_on_done(self, reason="done"):
        """Call the resume callback once, then clear it."""
        cb = self._on_done
        self._on_done = None
        if cb:
            try:
                cb(reason)
            except TypeError:
                try:
                    cb()
                except Exception as exc:
                    logger.error("collect on_done callback failed: %s", exc)
            except Exception as exc:
                logger.error("collect on_done callback failed: %s", exc)

    # ---- internal loop ----------------------------------------------------

    def _loop(self):
        done_naturally = False
        try:
            while not self._stop_event.is_set():
                ret, frame = self._cap.read()
                if not ret or frame is None:
                    time.sleep(0.01)
                    continue

                gray    = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                display = frame.copy()

                faces = self._cascade.detectMultiScale(
                    gray, scaleFactor=1.3, minNeighbors=5, minSize=(30, 30)
                )

                if len(faces) > 0:
                    # Use the largest face only
                    areas = [w * h for (x, y, w, h) in faces]
                    fx, fy, fw, fh = faces[int(np.argmax(areas))]

                    # Draw rectangle
                    cv2.rectangle(display, (fx, fy), (fx + fw, fy + fh), (168, 85, 247), 2)

                    # Crop face ROI
                    face_roi = gray[fy:fy + fh, fx:fx + fw]
                    if face_roi.size > 0 and self._collected < self._max_images:
                        for aug in augment_crop(face_roi):
                            if self._collected >= self._max_images:
                                break
                            filename = f"{self._person_id}_{self._next_idx}.jpg"
                            filepath = os.path.join(self._data_path, filename)
                            cv2.imwrite(filepath, aug)
                            self._collected  += 1
                            self._next_idx   += 1

                        # Emit progress
                        self._sio.emit("collect:progress", {
                            "collected":  self._collected,
                            "max_images": self._max_images,
                            "person_id":  self._person_id,
                            "name":       self._person_name,
                        })

                # Progress overlay
                label = f"Collected: {self._collected}/{self._max_images}"
                cv2.putText(display, label, (10, 30),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.8, (168, 85, 247), 2)

                # Encode to JPEG for preview
                ret_enc, jpeg = cv2.imencode(
                    ".jpg", display, [cv2.IMWRITE_JPEG_QUALITY, 80]
                )
                if ret_enc:
                    with self._frame_lock:
                        self._jpeg_buffer = jpeg.tobytes()

                # Done?
                if self._collected >= self._max_images:
                    self._sio.emit("collect:done", {
                        "person_id":  self._person_id,
                        "name":       self._person_name,
                        "collected":  self._collected,
                    })
                    done_naturally = True
                    break

                time.sleep(0.03)
        except Exception as exc:
            logger.error("Collector loop error: %s", exc)
            self._sio.emit("collect:error", {"message": str(exc)})
        finally:
            # Cleanup regardless of how we exited
            if self._cap:
                self._cap.release()
                self._cap = None
            self.is_active    = False
            self._jpeg_buffer = None
            self._sio.emit("collect:status", self.get_status())
            if done_naturally:
                # Fire on_done so the server can start training / resume LM
                self._fire_on_done("done")
            elif not self._stop_event.is_set():
                # Loop exited unexpectedly without stop() being called
                self._fire_on_done("error")
