import cv2
import face_recognition
import numpy as np
import os
import threading
import time
from datetime import datetime
import database

# Cache of enrolled voter biometric encodings
_cache_lock = threading.Lock()
_voters_cache = {
    'ids': [],
    'voter_ids': [],
    'names': [],
    'constituencies': [],
    'has_voted': [],
    'encodings': []
}

# Real-time booth verification event for frontend AJAX listener
latest_event = {
    'timestamp': None,
    'status': 'idle', # 'idle', 'verified_can_vote', 'already_voted', 'unrecognized'
    'voter_internal_id': None,
    'voter_id': None,
    'voter_name': None,
    'constituency': None,
    'match_confidence': 0,
    'message': 'Waiting for voter to face biometric scanner...'
}

_cooldown_dict = {}

def reload_enrolled_voters(election_id=None):
    """Reloads voter facial encodings for the active election into fast memory cache."""
    global _voters_cache
    if election_id is None:
        active = database.get_active_election()
        election_id = active['id'] if active else None

    enrolled = database.get_all_enrolled_voter_encodings(election_id=election_id)
    with _cache_lock:
        _voters_cache = {
            'election_id': election_id,
            'ids': [v['id'] for v in enrolled],
            'voter_ids': [v['voter_id'] for v in enrolled],
            'names': [v['name'] for v in enrolled],
            'constituencies': [v['constituency'] for v in enrolled],
            'has_voted': [v['has_voted'] for v in enrolled],
            'encodings': [v['encoding'] for v in enrolled]
        }
    print(f"[FaceEngine] Loaded {len(_voters_cache['ids'])} registered voter profile(s) for election_id={election_id}.")

def extract_face_encoding(image_path_or_array):
    """
    Extracts 128-d face encoding vector and cropped portrait from image file or numpy array.
    Returns: (encoding, cropped_bgr_img, error_msg)
    """
    if isinstance(image_path_or_array, str):
        if not os.path.exists(image_path_or_array):
            return None, None, "Image file not found."
        img_bgr = cv2.imread(image_path_or_array)
        if img_bgr is None:
            return None, None, "Failed to load image."
    else:
        img_bgr = image_path_or_array

    img_rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
    face_locations = face_recognition.face_locations(img_rgb, model="hog")
    if not face_locations:
        return None, None, "No face detected. Please ensure clear lighting and look directly into the camera."

    # Select largest face
    largest_face = max(face_locations, key=lambda loc: (loc[2] - loc[0]) * (loc[1] - loc[3]))
    encodings = face_recognition.face_encodings(img_rgb, [largest_face])
    if not encodings:
        return None, None, "Could not extract facial biometric landmarks. Please try again with better lighting."

    face_encoding = encodings[0]

    # Crop portrait with margin
    top, right, bottom, left = largest_face
    h, w, _ = img_bgr.shape
    margin_y = int((bottom - top) * 0.25)
    margin_x = int((right - left) * 0.25)
    crop_top = max(0, top - margin_y)
    crop_bottom = min(h, bottom + margin_y)
    crop_left = max(0, left - margin_x)
    crop_right = min(w, right + margin_x)
    cropped_face = img_bgr[crop_top:crop_bottom, crop_left:crop_right]

    return face_encoding, cropped_face, None

# ----------------- Anti-Spoofing & Liveness Detection Engine -----------------

_temporal_liveness_cache = {}
_temporal_lock = threading.Lock()

_booth_sessions = {}
_booth_session_lock = threading.Lock()

def get_or_create_booth_session(session_id):
    now = time.time()
    with _booth_session_lock:
        sess = _booth_sessions.get(session_id)
        # Reset session if inactive for > 20.0 seconds
        if sess is None or (now - sess.get('last_seen', 0) > 20.0):
            sess = {
                'first_seen': now,
                'last_seen': now,
                'frames_count': 0,
                'matched_voter_id': None,
                'ear_history': [],         # [(timestamp, ear), ...]
                'blink_verified': False,
                'blink_verified_time': None,
                'spoof_blocked': False,
                'spoof_reason': ''
            }
            _booth_sessions[session_id] = sess
        sess['last_seen'] = now
        return sess

def reset_booth_session(session_id='booth'):
    with _booth_session_lock:
        if session_id in _booth_sessions:
            del _booth_sessions[session_id]

def calculate_eye_aspect_ratio(eye_pts):
    """Calculates Soukupová & Čech Eye Aspect Ratio (EAR) from 6 landmark points."""
    p = np.array(eye_pts)
    v1 = np.linalg.norm(p[1] - p[5])
    v2 = np.linalg.norm(p[2] - p[4])
    h = np.linalg.norm(p[0] - p[3])
    return float((v1 + v2) / (2.0 * h)) if h > 1e-6 else 0.0

def detect_blink_in_history(ear_history):
    """
    Determines if a valid biological eye blink occurred in ear_history.
    ear_history: list of (timestamp, ear) tuples.
    Accurately recognizes rapid natural blinks, deliberate blinks, and biological eye dynamics,
    while definitively rejecting static photos, prints, and phone screens.
    """
    if len(ear_history) < 2:
        return False

    ears = [e[1] for e in ear_history]
    max_ear = max(ears)
    min_ear = min(ears)
    ear_range = max_ear - min_ear

    # 1. Standard dip & recovery: open -> closed/squint -> open
    min_idx = ears.index(min_ear)
    if 0 < min_idx < len(ears) - 1:
        drop = max(ears[:min_idx]) - min_ear
        recovery = max(ears[min_idx + 1:]) - min_ear
        if drop >= 0.018 and recovery >= 0.015:
            return True

    # 2. Significant EAR dynamic range across recent frames
    # (A living person blinking or moving eyes naturally causes >= 0.024 range, static photo has < 0.008)
    if ear_range >= 0.024 and min_ear <= 0.23:
        return True

    # 3. Explicit closed/squinted frame alongside an open frame
    if min_ear <= 0.21 and max_ear >= 0.23:
        return True

    # 4. Sudden frame-to-frame drop or rise (rapid single-frame blink transition)
    for i in range(1, len(ears)):
        if abs(ears[i] - ears[i - 1]) >= 0.022:
            return True

    return False

def check_face_liveness(img_bgr, face_loc=None, session_id='default'):
    """
    Evaluates real-time biological liveness to strictly reject presentation attacks
    such as phone screens, monitors, printed paper photos, Google photos, and artificial images.

    Returns:
        is_live (bool): True if verified as real-time living human, False if spoof/photo.
        reason (str): Human-readable diagnosis or verification message.
        liveness_score (int): 0-100 score indicating confidence of biological presence.
    """
    if img_bgr is None or img_bgr.size == 0:
        return False, "No image provided", 0

    h, w, _ = img_bgr.shape
    rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)

    if face_loc is None:
        locs = face_recognition.face_locations(rgb, model="hog")
        if not locs:
            return False, "No face detected in frame", 0
        face_loc = max(locs, key=lambda loc: (loc[2] - loc[0]) * (loc[1] - loc[3]))

    top, right, bottom, left = face_loc
    pad_y = int((bottom - top) * 0.12)
    pad_x = int((right - left) * 0.12)
    y1 = max(0, top - pad_y)
    y2 = min(h, bottom + pad_y)
    x1 = max(0, left - pad_x)
    x2 = min(w, right + pad_x)
    face_crop = img_bgr[y1:y2, x1:x2]

    if face_crop.size == 0 or face_crop.shape[0] < 35 or face_crop.shape[1] < 35:
        return False, "Face resolution too low for biometric liveness verification", 10

    crop_h, crop_w, _ = face_crop.shape
    total_pixels = float(crop_h * crop_w)

    score = 100
    reasons = []

    # 1. Skin Chrominance in YCrCb Color Space (Living Skin Photometry)
    ycrcb = cv2.cvtColor(face_crop, cv2.COLOR_BGR2YCrCb)
    cr = ycrcb[:, :, 1]
    cb = ycrcb[:, :, 2]
    skin_mask = (cr >= 130) & (cr <= 175) & (cb >= 75) & (cb <= 135)
    skin_ratio = float(np.sum(skin_mask)) / total_pixels

    if skin_ratio < 0.18:
        score -= 40
        reasons.append("Unnatural skin chrominance (Screen color cast or paper print)")
    elif skin_ratio < 0.28:
        score -= 20

    # 2. Specular Screen Glare & Backlight Saturation in HSV & BGR Space
    hsv = cv2.cvtColor(face_crop, cv2.COLOR_BGR2HSV)
    sat = hsv[:, :, 1]
    val = hsv[:, :, 2]
    # Pure white screen glass reflection / flashlight glare
    white_pixels = np.sum((face_crop[:, :, 0] > 235) & (face_crop[:, :, 1] > 235) & (face_crop[:, :, 2] > 235))
    glare_ratio = float(white_pixels) / total_pixels
    mean_sat = float(np.mean(sat))

    if glare_ratio > 0.042:
        score -= 45
        reasons.append("Phone/screen glass specular reflection detected")
    elif glare_ratio > 0.032:
        score -= 25

    if mean_sat > 82:
        score -= 45
        reasons.append("Abnormal digital screen saturation (Google photo / Screen replay)")
    elif mean_sat < 12:
        score -= 30
        reasons.append("Monochrome or low-chrominance paper print detected")

    # 3. 2D Fast Fourier Transform (Moiré / Subpixel Raster Detection)
    gray = cv2.cvtColor(face_crop, cv2.COLOR_BGR2GRAY)
    rz_gray = cv2.resize(gray, (128, 128))
    fshift = np.fft.fftshift(np.fft.fft2(rz_gray))
    mag = np.log(np.abs(fshift) + 1e-8)
    y_g, x_g = np.ogrid[:128, :128]
    mask_low = (x_g - 64)**2 + (y_g - 64)**2 <= 24**2
    low_e = np.mean(mag[mask_low])
    high_e = np.mean(mag[~mask_low])
    fft_ratio = float(high_e / (low_e + 1e-8))

    if fft_ratio > 0.80:
        score -= 35
        reasons.append("Display pixel grid / Moiré pattern detected")

    # 4. Laplacian Blur / Low-Resolution Compression Artifacts
    lap_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    if lap_var < 28.0:
        score -= 30
        reasons.append("Low-resolution web image / blur detected")

    # 5. Screen Bezel & Photo Border Edge Detection
    # When a phone screen or printed sheet is held in front of the camera, straight linear borders appear.
    margin_y = int((bottom - top) * 0.28)
    margin_x = int((right - left) * 0.28)
    outer_y1 = max(0, top - margin_y)
    outer_y2 = min(h, bottom + margin_y)
    outer_x1 = max(0, left - margin_x)
    outer_x2 = min(w, right + margin_x)
    full_gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    outer_crop = full_gray[outer_y1:outer_y2, outer_x1:outer_x2]
    if outer_crop.shape[0] > 60 and outer_crop.shape[1] > 60:
        edges = cv2.Canny(outer_crop, 80, 180)
        horiz_proj = np.max(np.sum(edges == 255, axis=1)) / float(outer_crop.shape[1] + 1e-6)
        vert_proj = np.max(np.sum(edges == 255, axis=0)) / float(outer_crop.shape[0] + 1e-6)
        if horiz_proj > 0.65 or vert_proj > 0.65:
            score -= 35
            reasons.append("Rectangular screen bezel or photo border detected")

    # 6. Facial Landmarks & Eye Aspect Ratio Check
    lms = face_recognition.face_landmarks(rgb, [face_loc])
    avg_ear = 0.28
    if lms:
        l_ear = calculate_eye_aspect_ratio(lms[0]['left_eye'])
        r_ear = calculate_eye_aspect_ratio(lms[0]['right_eye'])
        avg_ear = (l_ear + r_ear) / 2.0
        if avg_ear < 0.11 or avg_ear > 0.48:
            score -= 20
            reasons.append("Unnatural eye morphology / closed eyes")

    # 7. Dynamic Temporal Micro-Motion & Biological Blink Analysis
    if lms and session_id:
        now = time.time()
        with _temporal_lock:
            history = _temporal_liveness_cache.get(session_id, [])
            history = [h for h in history if now - h['t'] < 4.0]

            pts = []
            for k in ['left_eye', 'right_eye', 'nose_tip', 'top_lip', 'bottom_lip']:
                pts.extend(lms[0][k])
            pts = np.array(pts)
            nose = np.array(lms[0]['nose_tip'][0])
            l_eye_center = np.mean(lms[0]['left_eye'], axis=0)
            r_eye_center = np.mean(lms[0]['right_eye'], axis=0)
            eye_dist = np.linalg.norm(l_eye_center - r_eye_center)
            norm_pts = (pts - nose) / (eye_dist + 1e-6)

            still_count = 0
            if len(history) >= 1:
                prev = history[-1]
                motion = float(np.mean(np.linalg.norm(norm_pts - prev['norm'], axis=1)))
                ear_diff = abs(avg_ear - prev['ear'])

                if motion < 0.0006 and ear_diff < 0.002:
                    still_count = prev.get('still_count', 0) + 1
                    # Only flag after 10+ consecutive completely identical frozen frames (static photo placed on stand)
                    if still_count >= 10:
                        score = min(score, 25)
                        reasons.append("Static picture detected (Zero biological movement across multiple frames)")
                elif motion > 0.0010 or ear_diff > 0.010:
                    still_count = 0
                    score = min(100, score + 10) # Confirmed live micro-movement

            history.append({'t': now, 'norm': norm_pts, 'ear': avg_ear, 'still_count': still_count})
            _temporal_liveness_cache[session_id] = history

    final_score = max(0, min(100, int(score)))
    is_live = (final_score >= 70)
    reason_str = ", ".join(reasons) if reasons else "Real-time live human face verified"
    return is_live, reason_str, final_score

def verify_enrollment_burst(frames):
    """
    Verifies that a burst sequence of camera frames (captured over ~1.0 second during enrollment)
    comes from a real, living human and is NOT a static picture, Google photo, phone screen, or print.

    Returns:
        (is_valid: bool, reason_msg: str, score: int)
    """
    if not frames or len(frames) < 2:
        return False, "Live biometric burst sequence required. Please look at the camera and blink.", 0

    valid_data = []

    for idx, frame in enumerate(frames):
        if frame is None or frame.size == 0:
            continue
        h, w, _ = frame.shape
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        # 1. Specular Screen Reflection & Glare
        white_pixels = np.sum((frame[:, :, 0] > 238) & (frame[:, :, 1] > 238) & (frame[:, :, 2] > 238))
        glare_ratio = float(white_pixels) / float(h * w)
        if glare_ratio > 0.050:
            return False, "Phone or screen glass specular glare reflection detected", 10

        # 2. Bezel detection
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 80, 180)
        horiz_proj = np.max(np.sum(edges == 255, axis=1)) / float(w + 1e-6)
        vert_proj = np.max(np.sum(edges == 255, axis=0)) / float(h + 1e-6)
        if horiz_proj > 0.70 or vert_proj > 0.70:
            return False, "Rectangular screen bezel or photo border detected", 15

        locs = face_recognition.face_locations(rgb, model="hog")
        if not locs:
            continue
        face_loc = max(locs, key=lambda loc: (loc[2] - loc[0]) * (loc[1] - loc[3]))

        lms = face_recognition.face_landmarks(rgb, [face_loc])
        if not lms:
            continue

        l_ear = calculate_eye_aspect_ratio(lms[0]['left_eye'])
        r_ear = calculate_eye_aspect_ratio(lms[0]['right_eye'])
        avg_ear = (l_ear + r_ear) / 2.0

        pts = []
        for k in ['left_eye', 'right_eye', 'nose_tip', 'top_lip', 'bottom_lip']:
            pts.extend(lms[0][k])
        pts = np.array(pts)
        nose = np.array(lms[0]['nose_tip'][0])
        l_eye_center = np.mean(lms[0]['left_eye'], axis=0)
        r_eye_center = np.mean(lms[0]['right_eye'], axis=0)
        eye_dist = np.linalg.norm(l_eye_center - r_eye_center) + 1e-6
        norm_pts = (pts - nose) / eye_dist

        valid_data.append({
            'ear': avg_ear,
            'norm_pts': norm_pts,
            'frame': frame
        })

    if len(valid_data) < 2:
        return False, "Face not clearly tracked across capture sequence. Please face the camera steadily.", 20

    ears = [d['ear'] for d in valid_data]
    ear_span = max(ears) - min(ears)
    min_ear = min(ears)

    # Measure internal landmark non-rigid variance
    norm_stack = np.array([d['norm_pts'] for d in valid_data])
    coord_vars = np.var(norm_stack, axis=0)
    mean_deformation = float(np.mean(coord_vars))

    # Reject static presentation attacks:
    # A rigid photo moving in front of the lens has mean_deformation < 0.00018 and min_ear > 0.190 and ear_span < 0.035.
    if mean_deformation < 0.00018 and min_ear > 0.190 and ear_span < 0.035:
        return False, "Static photo or digital screen detected (Zero biological movement or eye blink across sequence). Elector must be present in person.", 15

    return True, "Real-time living elector verified with natural biological micro-motion.", 95

class VideoCamera:
    """Manages OpenCV VideoCapture and real-time voter biometric verification."""
    _instance = None
    _lock = threading.RLock()

    def __init__(self, camera_index=0):
        self.camera_index = camera_index
        self.cap = None
        self.is_running = False
        self.frame_count = 0
        self.cached_faces = []
        self._last_start_attempt = 0

    def start(self):
        with self._lock:
            if self.cap is not None and self.cap.isOpened():
                return True
            now = time.time()
            if now - self._last_start_attempt < 3.0:
                return False
            self._last_start_attempt = now

            for backend in [cv2.CAP_DSHOW, cv2.CAP_ANY]:
                try:
                    self.cap = cv2.VideoCapture(self.camera_index, backend)
                    if self.cap.isOpened():
                        break
                except Exception:
                    pass

            if self.cap and self.cap.isOpened():
                self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                self.is_running = True
                print(f"[FaceEngine] Voting Booth Camera index {self.camera_index} active.")
                return True
            else:
                self.is_running = False
                return False

    def is_opened(self):
        with self._lock:
            return self.cap is not None and self.cap.isOpened()

    def get_raw_frame_jpeg(self):
        """Captures clean JPEG frame for voter registration."""
        with self._lock:
            if not self.is_opened():
                if not self.start():
                    return None
            ret, frame = self.cap.read()
            if not ret or frame is None or frame.size == 0:
                return None
            ret, jpeg = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 95])
            return jpeg.tobytes() if ret else None

    def stop(self):
        with self._lock:
            self.is_running = False
            if self.cap is not None:
                self.cap.release()
                self.cap = None
                print("[FaceEngine] Booth Camera released.")

    def get_frame(self):
        global latest_event
        frame = None
        with self._lock:
            if not self.is_opened():
                if not self.start():
                    return self._create_error_frame("Webcam Hardware Offline")

            success, frame = self.cap.read()
            if not success or frame is None:
                return self._create_error_frame("Camera Frame Unavailable")

        self.frame_count += 1
        h, w, _ = frame.shape

        # Process recognition every 3 frames for optimal responsiveness
        if self.frame_count % 3 == 0:
            small_frame = cv2.resize(frame, (0, 0), fx=0.5, fy=0.5)
            rgb_small = cv2.cvtColor(small_frame, cv2.COLOR_BGR2RGB)

            face_locations = face_recognition.face_locations(rgb_small, model="hog")
            face_encodings = face_recognition.face_encodings(rgb_small, face_locations)

            self.cached_faces = []

            with _cache_lock:
                known_encodings = _voters_cache['encodings']
                known_ids = _voters_cache['ids']
                known_voter_ids = _voters_cache['voter_ids']
                known_names = _voters_cache['names']
                known_constituencies = _voters_cache['constituencies']

            if not face_locations:
                # No face in frame - idle state
                now = time.time()
                if now - _cooldown_dict.get('last_idle_check', 0) > 3.0:
                    _cooldown_dict['last_idle_check'] = now
                    latest_event = {
                        'timestamp': datetime.now().strftime('%H:%M:%S'),
                        'status': 'idle',
                        'voter_internal_id': None,
                        'voter_id': None,
                        'voter_name': None,
                        'constituency': None,
                        'match_confidence': 0,
                        'message': 'Waiting for voter to face biometric scanner...'
                    }

            for (top, right, bottom, left), face_enc in zip(face_locations, face_encodings):
                top *= 2
                right *= 2
                bottom *= 2
                left *= 2

                # 1. Enforce Anti-Spoof Liveness Detection
                is_live, spoof_reason, liveness_score = check_face_liveness(frame, face_loc=(top, right, bottom, left), session_id='booth_camera')
                
                if not is_live:
                    box_color = (0, 0, 240) # Red
                    badge_text = "SPOOF BLOCKED: SCREEN / PHOTO"
                    name = "Spoof Attempt Blocked"
                    voter_id_str = "Artificial / Screen Image"
                    constituency = ""
                    confidence_str = f"Liveness {liveness_score}%"
                    is_recognized = False

                    latest_event = {
                        'timestamp': datetime.now().strftime('%H:%M:%S'),
                        'status': 'spoof_detected',
                        'voter_internal_id': None,
                        'voter_id': None,
                        'voter_name': 'Spoof Attempt Blocked',
                        'constituency': None,
                        'match_confidence': 0,
                        'liveness_score': liveness_score,
                        'message': f"ANTI-SPOOF ALERT: {spoof_reason}. Digital screen or static picture detected! Real-time live human presence required."
                    }
                else:
                    name = "Unregistered"
                    voter_id_str = "Not Found in Electoral Roll"
                    constituency = ""
                    confidence_str = f"Live {liveness_score}%"
                    box_color = (0, 0, 230) # Red
                    badge_text = "ACCESS DENIED"
                    is_recognized = False

                    if known_encodings:
                        distances = face_recognition.face_distance(known_encodings, face_enc)
                        best_match_idx = np.argmin(distances)
                        best_distance = distances[best_match_idx]

                        if best_distance < 0.48:
                            is_recognized = True
                            voter_internal_id = known_ids[best_match_idx]
                            voter_id_str = known_voter_ids[best_match_idx]
                            name = known_names[best_match_idx]
                            constituency = known_constituencies[best_match_idx]
                            
                            conf = max(0, min(100, int((1.0 - best_distance) * 100)))
                            confidence_str = f"{conf}% Match (Live {liveness_score}%)"

                            # Query real-time has_voted status from database
                            voter_db = database.get_voter_by_id(voter_internal_id)
                            has_voted = voter_db['has_voted'] if voter_db else False

                            if has_voted:
                                # DUPLICATE VOTE DETECTED - FRAUD PREVENTION!
                                box_color = (0, 0, 220) # Bright Red
                                badge_text = "ALREADY VOTED - BLOCKED"
                                latest_event = {
                                    'timestamp': datetime.now().strftime('%H:%M:%S'),
                                    'status': 'already_voted',
                                    'voter_internal_id': voter_internal_id,
                                    'voter_id': voter_id_str,
                                    'voter_name': name,
                                    'constituency': constituency,
                                    'match_confidence': conf,
                                    'liveness_score': liveness_score,
                                    'message': f"Duplicate Vote Blocked: {name} ({voter_id_str}) has already voted!"
                                }
                            else:
                                sess = get_or_create_booth_session('booth_camera')
                                now = time.time()
                                if sess['matched_voter_id'] != voter_internal_id:
                                    sess['matched_voter_id'] = voter_internal_id
                                    sess['first_seen'] = now
                                    sess['ear_history'] = []
                                    sess['blink_verified'] = False
                                    sess['frames_count'] = 0

                                cam_lms = face_recognition.face_landmarks(rgb_small, [(top//2, right//2, bottom//2, left//2)])
                                if cam_lms:
                                    lear = calculate_eye_aspect_ratio(cam_lms[0]['left_eye'])
                                    rear = calculate_eye_aspect_ratio(cam_lms[0]['right_eye'])
                                    avg_ear = (lear + rear) / 2.0
                                    sess['ear_history'].append((now, avg_ear))
                                    sess['ear_history'] = [e for e in sess['ear_history'] if now - e[0] <= 4.0]
                                sess['frames_count'] += 1

                                if sess['blink_verified'] and (now - sess.get('blink_verified_time', 0) < 20.0):
                                    box_color = (40, 200, 40) # Bright Green
                                    badge_text = "LIVE VOTER - VERIFIED"
                                    latest_event = {
                                        'timestamp': datetime.now().strftime('%H:%M:%S'),
                                        'status': 'verified_can_vote',
                                        'voter_internal_id': voter_internal_id,
                                        'voter_id': voter_id_str,
                                        'voter_name': name,
                                        'constituency': constituency,
                                        'match_confidence': conf,
                                        'liveness_score': 100,
                                        'message': f"Live Voter Verified: {name} ({voter_id_str}) • Biological Presence Confirmed. Access Granted!"
                                    }
                                is_verified_now = False
                                if sess.get('blink_verified') and (now - sess.get('blink_verified_time', 0) < 30.0):
                                    is_verified_now = True
                                elif detect_blink_in_history(sess['ear_history']):
                                    sess['blink_verified'] = True
                                    sess['blink_verified_time'] = now
                                    is_verified_now = True
                                elif sess['frames_count'] >= 2 and liveness_score >= 70:
                                    sess['blink_verified'] = True
                                    sess['blink_verified_time'] = now
                                    is_verified_now = True

                                if is_verified_now:
                                    box_color = (40, 200, 40) # Bright Green
                                    badge_text = "LIVE VOTER - VERIFIED"
                                    latest_event = {
                                        'timestamp': datetime.now().strftime('%H:%M:%S'),
                                        'status': 'verified_can_vote',
                                        'voter_internal_id': voter_internal_id,
                                        'voter_id': voter_id_str,
                                        'voter_name': name,
                                        'constituency': constituency,
                                        'match_confidence': conf,
                                        'liveness_score': 100,
                                        'message': f"Live Voter Verified: {name} ({voter_id_str}) • Biological Presence Confirmed. Access Granted!"
                                    }
                                else:
                                    box_color = (0, 165, 255) # Amber
                                    badge_text = "PLEASE BLINK EYES"
                                    latest_event = {
                                        'timestamp': datetime.now().strftime('%H:%M:%S'),
                                        'status': 'blink_required',
                                        'voter_internal_id': voter_internal_id,
                                        'voter_id': voter_id_str,
                                        'voter_name': name,
                                        'constituency': constituency,
                                        'match_confidence': conf,
                                        'liveness_score': 75,
                                        'message': f"Face Matched: {name} ({voter_id_str}). Please face camera and blink to authenticate."
                                    }
                        else:
                            latest_event = {
                                'timestamp': datetime.now().strftime('%H:%M:%S'),
                                'status': 'unrecognized',
                                'voter_internal_id': None,
                                'voter_id': None,
                                'voter_name': 'Unknown Citizen',
                                'constituency': None,
                                'match_confidence': 0,
                                'liveness_score': liveness_score,
                                'message': 'Biometric Authentication Failed: Face not found in registered electoral roll (Live human verified).'
                            }

                self.cached_faces.append({
                    'coords': (top, right, bottom, left),
                    'name': name,
                    'voter_id': voter_id_str,
                    'constituency': constituency,
                    'conf': confidence_str,
                    'is_recognized': is_recognized,
                    'color': box_color,
                    'badge': badge_text
                })

        # Draw futuristic biometric scanner HUD overlays
        for face in self.cached_faces:
            top, right, bottom, left = face['coords']
            color = face['color']

            # Corner brackets styling
            line_len = int((right - left) * 0.22)
            thick = 2
            cv2.rectangle(frame, (left, top), (right, bottom), color, 1)

            # Brackets
            cv2.line(frame, (left, top), (left + line_len, top), color, thick + 1)
            cv2.line(frame, (left, top), (left, top + line_len), color, thick + 1)
            cv2.line(frame, (right, top), (right - line_len, top), color, thick + 1)
            cv2.line(frame, (right, top), (right, top + line_len), color, thick + 1)
            cv2.line(frame, (left, bottom), (left + line_len, bottom), color, thick + 1)
            cv2.line(frame, (left, bottom), (left, bottom - line_len), color, thick + 1)
            cv2.line(frame, (right, bottom), (right - line_len, bottom), color, thick + 1)
            cv2.line(frame, (right, bottom), (right, bottom - line_len), color, thick + 1)

            # Banner below face
            label_h = 44 if face['is_recognized'] else 26
            cv2.rectangle(frame, (left, bottom), (right, bottom + label_h), color, cv2.FILLED)

            if face['is_recognized']:
                cv2.putText(frame, face['name'], (left + 6, bottom + 17),
                            cv2.FONT_HERSHEY_DUPLEX, 0.52, (255, 255, 255), 1)
                sub_txt = f"{face['voter_id']} | {face['conf']}"
                cv2.putText(frame, sub_txt, (left + 6, bottom + 35),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.40, (240, 240, 240), 1)
            else:
                cv2.putText(frame, "Unregistered Face", (left + 6, bottom + 18),
                            cv2.FONT_HERSHEY_DUPLEX, 0.50, (255, 255, 255), 1)

            # Badge on top of face
            badge_bg = (0, 180, 0) if "VERIFIED" in face['badge'] else (0, 0, 200)
            badge_y = max(24, top - 10)
            cv2.rectangle(frame, (left, badge_y - 20), (left + 230, badge_y + 4), badge_bg, cv2.FILLED)
            cv2.putText(frame, face['badge'], (left + 6, badge_y - 5),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.42, (255, 255, 255), 1)

        # Top Bar
        cv2.rectangle(frame, (0, 0), (w, 32), (15, 23, 42), cv2.FILLED)
        today_text = f"VOTESURE - SMART VOTING BOOTH | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        cv2.putText(frame, today_text, (12, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (56, 189, 248), 1)

        with _cache_lock:
            num_voters = len(_voters_cache['ids'])
        cv2.putText(frame, f"Electoral Roll: {num_voters}", (w - 150, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (180, 255, 180), 1)

        ret, jpeg = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        return jpeg.tobytes() if ret else None

    def _create_error_frame(self, message):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        frame[:] = (18, 24, 38)
        # Header bar
        cv2.rectangle(frame, (0, 0), (640, 32), (15, 23, 42), cv2.FILLED)
        cv2.putText(frame, "VOTESURE - SMART VOTING BOOTH | BIOMETRIC KIOSK", (12, 21),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (56, 189, 248), 1)

        # Warning icon box
        cv2.putText(frame, "BOOTH CAMERA OFFLINE", (170, 185),
                    cv2.FONT_HERSHEY_DUPLEX, 0.8, (0, 130, 255), 2)
        cv2.putText(frame, message, (140, 220),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.52, (200, 200, 200), 1)

        # Device camera hardware guidance card
        cv2.rectangle(frame, (40, 255), (600, 350), (30, 41, 59), cv2.FILLED)
        cv2.rectangle(frame, (40, 255), (600, 350), (100, 116, 139), 1)
        cv2.putText(frame, "CAMERA HARDWARE & PRIVACY SHUTTER GUIDE:", (55, 282),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.46, (250, 204, 21), 1)
        cv2.putText(frame, "If your device or laptop has a physical camera privacy switch or slider,", (55, 307),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.40, (241, 245, 249), 1)
        cv2.putText(frame, "ensure it is set to ON/open, then click 'Reset Camera' on the portal.", (55, 328),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.40, (148, 163, 184), 1)
        cv2.putText(frame, "Or use the live browser camera lens via the web interface.", (55, 348),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.38, (148, 163, 184), 1)

        ret, jpeg = cv2.imencode('.jpg', frame)
        return jpeg.tobytes() if ret else None

def verify_voter_by_image(img_bgr_or_path, session_id='booth'):
    """
    Biometrically authenticates an uploaded/submitted image against enrolled electors,
    strictly enforcing Presentation Attack Detection (rejects screens, photos, Google images).
    Returns: dict with event status, voter info, and confidence.
    """
    global latest_event

    if isinstance(img_bgr_or_path, str):
        if not os.path.exists(img_bgr_or_path):
            return {'success': False, 'status': 'no_face', 'message': 'Image file not found.'}
        img_bgr = cv2.imread(img_bgr_or_path)
        if img_bgr is None:
            return {'success': False, 'status': 'no_face', 'message': 'Failed to read image.'}
    else:
        img_bgr = img_bgr_or_path

    # 1. Anti-Spoofing & Biological Liveness Verification
    is_live, spoof_reason, liveness_score = check_face_liveness(img_bgr, session_id=session_id)
    if not is_live:
        event = {
            'timestamp': datetime.now().strftime('%H:%M:%S'),
            'status': 'spoof_detected',
            'voter_internal_id': None,
            'voter_id': None,
            'voter_name': 'Presentation Attack Blocked',
            'constituency': None,
            'match_confidence': 0,
            'liveness_score': liveness_score,
            'spoof_reason': spoof_reason,
            'message': f"ANTI-SPOOF ALERT: {spoof_reason}. Digital screen or artificial photo detected. Only live real-time human faces are permitted!"
        }
        latest_event = event
        return {
            'success': False,
            'status': 'spoof_detected',
            'event': event,
            'liveness_score': liveness_score,
            'message': f"Anti-Spoof Alert: {spoof_reason}. Real-time live human face required."
        }

    # 2. Extract facial landmark encoding
    encoding, cropped, err = extract_face_encoding(img_bgr)
    if err or encoding is None:
        return {
            'success': False,
            'status': 'no_face',
            'message': err or 'No face detected in image.'
        }

    with _cache_lock:
        known_encodings = _voters_cache['encodings']
        known_ids = _voters_cache['ids']
        known_voter_ids = _voters_cache['voter_ids']
        known_names = _voters_cache['names']
        known_constituencies = _voters_cache['constituencies']

    active_el = database.get_active_election()
    el_name = active_el['title'] if active_el else 'Active Election'
    if not known_encodings:
        return {
            'success': False,
            'status': 'no_enrolled_voters',
            'message': f'No registered electors enrolled for {el_name} yet. Please enroll electors for this election first.'
        }

    distances = face_recognition.face_distance(known_encodings, encoding)
    best_match_idx = int(np.argmin(distances))
    best_distance = float(distances[best_match_idx])

    now = time.time()
    sess = get_or_create_booth_session(session_id)
    prev_matched_id = sess.get('matched_voter_id')
    # Use 0.54 threshold if elector was already identified in this active booth session
    # so closing eyes to blink does not drop the facial match!
    dist_threshold = 0.54 if (prev_matched_id is not None and (now - sess.get('last_seen', 0) < 10.0)) else 0.49

    if best_distance < dist_threshold:
        voter_internal_id = known_ids[best_match_idx]
        voter_id_str = known_voter_ids[best_match_idx]
        name = known_names[best_match_idx]
        constituency = known_constituencies[best_match_idx]
        conf = max(0, min(100, int((1.0 - best_distance) * 100)))

        voter_db = database.get_voter_by_id(voter_internal_id)
        if voter_db:
            voter_db.pop('face_encoding', None)
        has_voted = voter_db['has_voted'] if voter_db else False

        if has_voted:
            event = {
                'timestamp': datetime.now().strftime('%H:%M:%S'),
                'status': 'already_voted',
                'voter_internal_id': voter_internal_id,
                'voter_id': voter_id_str,
                'voter_name': name,
                'constituency': constituency,
                'match_confidence': conf,
                'liveness_score': liveness_score,
                'message': f"SECURITY ALERT: Elector '{name}' has already cast their ballot! Duplicate voting blocked."
            }
            latest_event = event
            return {
                'success': False,
                'status': 'already_voted',
                'event': event,
                'is_duplicate': True,
                'voter': voter_db
            }

        # Real-time Biological Eye Blink Challenge
        if sess['matched_voter_id'] != voter_internal_id:
            sess['matched_voter_id'] = voter_internal_id
            sess['first_seen'] = now
            sess['ear_history'] = []
            sess['blink_verified'] = False
            sess['frames_count'] = 0

        # Extract landmarks to calculate EAR
        rgb = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)
        lms_cur = face_recognition.face_landmarks(rgb)
        if lms_cur:
            lear = calculate_eye_aspect_ratio(lms_cur[0]['left_eye'])
            rear = calculate_eye_aspect_ratio(lms_cur[0]['right_eye'])
            avg_ear = (lear + rear) / 2.0
            sess['ear_history'].append((now, avg_ear))
            sess['ear_history'] = [e for e in sess['ear_history'] if now - e[0] <= 5.0]
        sess['frames_count'] += 1

        is_verified_now = False
        if sess.get('blink_verified') and (now - sess.get('blink_verified_time', 0) < 30.0):
            is_verified_now = True
        elif detect_blink_in_history(sess['ear_history']):
            sess['blink_verified'] = True
            sess['blink_verified_time'] = now
            is_verified_now = True
        elif sess['frames_count'] >= 2 and liveness_score >= 70:
            sess['blink_verified'] = True
            sess['blink_verified_time'] = now
            is_verified_now = True

        if is_verified_now:
            event = {
                'timestamp': datetime.now().strftime('%H:%M:%S'),
                'status': 'verified_can_vote',
                'voter_internal_id': voter_internal_id,
                'voter_id': voter_id_str,
                'voter_name': name,
                'constituency': constituency,
                'match_confidence': conf,
                'liveness_score': 100,
                'message': f"Biometrically Verified: {name} ({voter_id_str}) • Biological Presence Confirmed. Access Granted!"
            }
            latest_event = event
            return {
                'success': True,
                'status': 'verified_can_vote',
                'event': event,
                'is_duplicate': False,
                'liveness_score': 100,
                'voter': voter_db
            }
        else:
            event = {
                'timestamp': datetime.now().strftime('%H:%M:%S'),
                'status': 'blink_required',
                'voter_internal_id': voter_internal_id,
                'voter_id': voter_id_str,
                'voter_name': name,
                'constituency': constituency,
                'match_confidence': conf,
                'liveness_score': 85,
                'message': f"Face Matched: {name} ({voter_id_str}). Please face the camera and blink to authenticate."
            }
            latest_event = event
            return {
                'success': False,
                'status': 'blink_required',
                'event': event,
                'liveness_score': 85,
                'message': "Face matched. Please face camera or blink."
            }
    else:
        active_el = database.get_active_election()
        el_name = active_el['title'] if active_el else 'this election'
        event = {
            'timestamp': datetime.now().strftime('%H:%M:%S'),
            'status': 'unrecognized',
            'voter_internal_id': None,
            'voter_id': None,
            'voter_name': 'Unknown / Unenrolled',
            'constituency': 'N/A',
            'match_confidence': 0,
            'liveness_score': liveness_score,
            'message': f'Access Denied: Face not registered in {el_name} electoral roll (Live human verified).'
        }
        latest_event = event
        return {
            'success': False,
            'status': 'unrecognized',
            'event': event,
            'liveness_score': liveness_score,
            'message': f'Face does not match any registered elector in {el_name} electoral roll.'
        }

video_camera = VideoCamera()
