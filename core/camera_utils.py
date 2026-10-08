"""
core/camera_utils.py
Shared camera backend constant.

cv2.CAP_DSHOW on Windows, cv2.CAP_ANY elsewhere.
Import ``CAM_BACKEND`` from this module in detector.py, collector.py
and server.py so the choice is defined in exactly one place.
"""

import sys
import cv2

CAM_BACKEND = cv2.CAP_DSHOW if sys.platform.startswith("win") else cv2.CAP_ANY
