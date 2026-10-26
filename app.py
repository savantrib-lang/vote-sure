import os
import re
import time
import secrets
import json
import threading
from dotenv import load_dotenv

# Load environment variables from .env file immediately
load_dotenv()

import cv2
import base64
import numpy as np
import pandas as pd
from datetime import datetime, date
from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, session, Response, jsonify, send_file, send_from_directory
)
import database
import face_engine
import random

app = Flask(__name__)
secret_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', '.session_secret')
os.makedirs(os.path.dirname(secret_path), exist_ok=True)
if not os.path.exists(secret_path):
    with open(secret_path, 'w') as secret_file:
        secret_file.write(secrets.token_hex(32))
with open(secret_path) as secret_file:
    app.secret_key = os.environ.get('SECRET_KEY') or secret_file.read().strip()
app.config.update(
    MAX_CONTENT_LENGTH=8 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax'
)

# Configure upload directory
UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'uploads', 'voters')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Initialize DB and preload facial biometrics into engine
database.init_db()
face_engine.reload_enrolled_voters()

# ----------------- Security Engine: Rate Limiting & Origin Defense -----------------

_failed_attempts = {}
_last_verified_booth_voter = {}
_security_lock = threading.Lock()

def check_rate_limit(key, max_attempts=5, window_sec=60):
    now = time.time()
    with _security_lock:
        attempts = _failed_attempts.get(key, [])
        attempts = [t for t in attempts if now - t < window_sec]
        if len(attempts) >= max_attempts:
            retry_after = int(window_sec - (now - attempts[0]))
            return False, max(1, retry_after)
        return True, 0

def record_failed_attempt(key):
    now = time.time()
    with _security_lock:
        attempts = _failed_attempts.get(key, [])
        attempts = [t for t in attempts if now - t < 60]
        attempts.append(now)
        _failed_attempts[key] = attempts

def clear_failed_attempts(key):
    with _security_lock:
        _failed_attempts.pop(key, None)

def get_client_ip():
    return request.headers.get('CF-Connecting-IP') or request.headers.get('X-Forwarded-For', request.remote_addr).split(',')[0].strip()

@app.before_request
def security_pre_check():
    # 1. Session Inactivity Timeout (30 minutes)
    if 'user_id' in session:
        now = time.time()
        last_active = session.get('last_active', now)
        if now - last_active > 1800:
            session.clear()
            flash("Your session has expired due to inactivity. Please sign in again.", "warning")
            return redirect(url_for('login'))
        session['last_active'] = now

    # 2. Cross-Site Request Origin Protection for POST/PUT/DELETE requests
    if request.method in ('POST', 'PUT', 'DELETE'):
        origin = request.headers.get('Origin')
        referer = request.headers.get('Referer')
        target = origin or referer
        if target:
            target_host = target.split('://', 1)[-1].split('/', 1)[0].lower()
            current_host = request.host.lower()
            trusted_suffixes = ('.trycloudflare.com', 'localhost', '127.0.0.1', '10.242.97.220')
            is_trusted = (target_host == current_host) or any(target_host.endswith(s) or s in target_host for s in trusted_suffixes)
            if not is_trusted:
                if request.is_json or request.path.startswith('/api/'):
                    return jsonify({'success': False, 'message': 'Cross-site request blocked.'}), 403
                flash("Security Alert: Untrusted cross-site request origin rejected.", "danger")
                return redirect(url_for('dashboard')), 403

@app.after_request
def apply_security_headers(response):
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Permissions-Policy'] = 'camera=(self), microphone=(), geolocation=()'
    response.headers['Content-Security-Policy'] = "default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob: https:; img-src 'self' data: blob: https:; media-src 'self' blob:;"

    if request.endpoint in ('ballot', 'results', 'audit_logs', 'voters_list'):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
    return response

@app.context_processor
def inject_global_data():
    try:
        active_el = database.get_active_election()
    except Exception:
        active_el = None
    return {
        'now_year': datetime.now().year,
        'active_election': active_el
    }

# ----------------- Decorators & Helpers -----------------

def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash("Please sign in as Election Official or Polling Officer.", "warning")
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    from functools import wraps
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash("Please sign in as Election Official or Administrator.", "warning")
            return redirect(url_for('login'))
        if session.get('role') not in ('admin', 'chief_electoral_officer', 'principal', 'college_election_officer'):
            flash("Unauthorized: This action is strictly reserved for Election Administrators, Chief Electoral Officers, and Principals.", "danger")
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function

# ----------------- Authentication Routes -----------------

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        client_ip = get_client_ip()
        rate_key = f"login_{client_ip}"
        allowed, retry_after = check_rate_limit(rate_key, max_attempts=5, window_sec=60)
        if not allowed:
            flash(f"Security Alert: Too many failed login attempts. Account temporarily locked for {retry_after} seconds.", "danger")
            return render_template('login.html'), 429

        username = request.form.get('username', '').strip()[:40]
        password = request.form.get('password', '').strip()[:100]

        user = database.verify_user(username, password)
        if user:
            clear_failed_attempts(rate_key)
            session.clear()
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']
            session['full_name'] = user['full_name']
            session['last_active'] = time.time()
            flash(f"Welcome, {user['full_name']}! Election portal access granted.", "success")
            return redirect(url_for('dashboard'))
        else:
            record_failed_attempt(rate_key)
            flash("Invalid credentials. Please verify username and password.", "danger")

    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    """Allows new election officials to register with role-specific designations: Chief Electoral Officer, Polling Officer, School/College Election Officer, Principal, or Administrator."""
    if request.method == 'POST':
        client_ip = get_client_ip()
        allowed, retry_after = check_rate_limit(f"register_{client_ip}", max_attempts=10, window_sec=60)
        if not allowed:
            flash(f"Security Alert: Too many registration attempts. Please wait {retry_after} seconds.", "danger")
            return render_template('register.html'), 429

        requested_role = request.form.get('role', 'officer').strip().lower()
        if requested_role == 'polling_officer':
            requested_role = 'officer'

        allowed_roles = [
            'chief_electoral_officer',
            'officer',
            'college_election_officer',
            'principal',
            'admin'
        ]

        role = requested_role if requested_role in allowed_roles else 'officer'
        full_name = re.sub(r'[<>{}\\\/]', '', request.form.get('full_name', '').strip())[:60]
        institution_name = re.sub(r'[<>{}\\\/]', '', request.form.get('institution_name', '').strip())[:80]
        email = re.sub(r'[<>{}\\\/]', '', request.form.get('email', '').strip())[:80]
        contact = re.sub(r'[<>{}\\\/]', '', request.form.get('contact', '').strip())[:30]
        username = re.sub(r'[^A-Za-z0-9_]', '', request.form.get('username', '').strip())[:30]
        password = request.form.get('password', '').strip()[:100]
        confirm_password = request.form.get('confirm_password', '').strip()[:100]

        if not full_name or not username or not password or not email or not contact:
            flash("All required fields (Full Name, Email, Phone Number, Username, and Password) must be completed.", "danger")
            return render_template('register.html')

        if len(password) < 4:
            flash("Password must be at least 4 characters for election portal security.", "danger")
            return render_template('register.html')

        if password != confirm_password:
            flash("Passwords do not match. Please verify your password.", "danger")
            return render_template('register.html')

        # Format display name with institution / college / station if provided
        display_full_name = full_name
        if institution_name and institution_name.lower() not in full_name.lower():
            display_full_name = f"{full_name} ({institution_name})"

        user_id, err = database.create_user(
            username=username,
            password=password,
            role=role,
            full_name=display_full_name,
            email=email,
            contact=contact
        )
        if err:
            flash(f"Registration failed: {err}", "danger")
            return render_template('register.html')

        role_labels = {
            'chief_electoral_officer': 'Chief Electoral Officer (VoteSure National Commission)',
            'officer': 'Polling Booth Officer',
            'college_election_officer': 'School / College Election Officer',
            'principal': 'College / School Principal',
            'admin': 'Administrator (Chief Election Commissioner)'
        }
        role_label = role_labels.get(role, 'Election Official')
        flash(f"Account registered successfully! Welcome {role_label} '{username}'. Please sign in.", "success")
        return redirect(url_for('login'))

    return render_template('register.html')

@app.route('/logout')
def logout():
    session.clear()
    flash("You have been signed out from the voting portal.", "info")
    return redirect(url_for('login'))

# ----------------- Dashboard & Analytics -----------------

@app.route('/uploads/<path:filename>')
def serve_uploads(filename):
    uploads_dir = os.path.abspath(os.path.join(app.root_path, 'static', 'uploads'))
    target_path = os.path.abspath(os.path.join(uploads_dir, filename))
    if not target_path.startswith(uploads_dir) or not os.path.exists(target_path):
        return "Access Denied / File Not Found", 404
    return send_from_directory(uploads_dir, filename)

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    all_elections = database.get_all_elections()
    active_election = database.get_active_election()
    selected_id = request.args.get('election_id', type=int)
    if not selected_id:
        selected_id = active_election['id'] if active_election else (all_elections[0]['id'] if all_elections else 1)
    current_election = database.get_election_by_id(selected_id) or active_election

    stats = database.get_election_dashboard_stats(election_id=selected_id)
    candidates = database.get_candidates_for_election(election_id=selected_id)
    return render_template('dashboard.html', stats=stats, candidates=candidates, all_elections=all_elections, current_election=current_election, selected_id=selected_id)

@app.route('/results', methods=['GET', 'POST'])
@login_required
def results():
    role = session.get('role')
    # Access authorized for Chief Electoral Officer, College Principal, School/College Election Officer, Polling Officer, and Administrator
    if role not in ('chief_electoral_officer', 'principal', 'college_election_officer', 'officer', 'admin'):
        flash("Restricted Access: Only authorized election officers and principals can access the results calculation portal.", "danger")
        return redirect(url_for('dashboard'))

    all_elections = database.get_all_elections()
    active_election = database.get_active_election()
    selected_id = request.args.get('election_id', type=int)
    if not selected_id and request.method == 'POST':
        selected_id = request.form.get('election_id', type=int)

    if not selected_id:
        selected_id = active_election['id'] if active_election else (all_elections[0]['id'] if all_elections else 1)

    current_election = database.get_election_by_id(selected_id) or active_election

    # Track independent seal lock status per election
    now = time.time()
    election_locks = {}
    for el in all_elections:
        el_id = el['id']
        is_unlocked = bool(session.get(f'results_unlocked_{el_id}'))
        # Auto-timeout after 10 mins (600s)
        if is_unlocked and (now - session.get(f'results_unlocked_at_{el_id}', 0) > 600):
            session.pop(f'results_unlocked_{el_id}', None)
            session.pop(f'results_unlocked_at_{el_id}', None)
            session.pop(f'results_unlocked_by_{el_id}', None)
            is_unlocked = False
        election_locks[el_id] = is_unlocked

    is_current_unlocked = election_locks.get(selected_id, False)
    stats = database.get_election_dashboard_stats(election_id=selected_id)

    if request.method == 'POST':
        client_ip = get_client_ip()
        rate_key = f"results_pw_{selected_id}_{client_ip}"
        allowed, retry_after = check_rate_limit(rate_key, max_attempts=5, window_sec=60)
        if not allowed:
            flash(f"Security Alert: Too many failed password attempts. Sealed results access locked for {retry_after} seconds.", "danger")
            return render_template(
                'results_locked.html',
                stats=stats,
                current_election=current_election,
                all_elections=all_elections,
                selected_id=selected_id,
                election_locks=election_locks
            ), 429

        password = request.form.get('officer_password', '').strip()[:100]
        username = session.get('username')
        user = database.verify_user(username, password)

        if not user:
            record_failed_attempt(rate_key)
            flash("Access Denied: Incorrect password. The vote calculation list remains strictly sealed.", "danger")
            return render_template(
                'results_locked.html',
                stats=stats,
                current_election=current_election,
                all_elections=all_elections,
                selected_id=selected_id,
                election_locks=election_locks
            ), 401

        # Check election-specific authorization
        is_general = current_election.get('election_type') == 'general'
        if is_general:
            # VoteSure National Public Election -> Chief Electoral Officer or Admin
            if user['role'] not in ('chief_electoral_officer', 'admin'):
                flash("Access Denied: VoteSure National Election results can strictly be unsealed only by the Chief Electoral Officer or Administrator.", "danger")
                return render_template(
                    'results_locked.html',
                    stats=stats,
                    current_election=current_election,
                    all_elections=all_elections,
                    selected_id=selected_id,
                    election_locks=election_locks
                ), 403
            auth_role_title = "Chief Electoral Officer" if user['role'] == 'chief_electoral_officer' else "Administrator"
        else:
            # College / School Election -> Principal, College Election Officer, Polling Officer, or Admin
            if user['role'] not in ('principal', 'college_election_officer', 'officer', 'admin', 'chief_electoral_officer'):
                flash("Access Denied: Student union election results can strictly be unsealed only by the College Principal, School/College Election Officer, or Administrator.", "danger")
                return render_template(
                    'results_locked.html',
                    stats=stats,
                    current_election=current_election,
                    all_elections=all_elections,
                    selected_id=selected_id,
                    election_locks=election_locks
                ), 403
            if user['role'] == 'principal':
                auth_role_title = "College Principal"
            elif user['role'] == 'college_election_officer':
                auth_role_title = "School/College Election Officer"
            elif user['role'] == 'officer':
                auth_role_title = "Polling Officer"
            elif user['role'] == 'chief_electoral_officer':
                auth_role_title = "Chief Electoral Officer"
            else:
                auth_role_title = "Administrator"

        clear_failed_attempts(rate_key)
        session[f'results_unlocked_{selected_id}'] = True
        session[f'results_unlocked_at_{selected_id}'] = time.time()
        session[f'results_unlocked_by_{selected_id}'] = f"{user['full_name']} ({auth_role_title})"
        flash(f"{auth_role_title} authenticated! '{current_election['title']}' ballot vault unsealed.", "success")
        return redirect(url_for('results', election_id=selected_id))

    if not is_current_unlocked:
        return render_template(
            'results_locked.html',
            stats=stats,
            current_election=current_election,
            all_elections=all_elections,
            selected_id=selected_id,
            election_locks=election_locks
        )

    results_data = database.get_election_results_data(election_id=selected_id)
    voted_electors = database.get_all_votes_audit_trail(election_id=selected_id)
    integrity_report = database.verify_database_integrity()
    unlocked_by = session.get(f'results_unlocked_by_{selected_id}', 'Authorized Election Authority')
    return render_template(
        'results.html',
        stats=stats,
        results=results_data,
        voted_electors=voted_electors,
        integrity_report=integrity_report,
        all_elections=all_elections,
        current_election=current_election,
        selected_id=selected_id,
        election_locks=election_locks,
        unlocked_by=unlocked_by
    )

@app.route('/results/lock', methods=['POST'])
@login_required
def lock_results():
    election_id = request.form.get('election_id', type=int) or request.args.get('election_id', type=int)
    if election_id:
        session.pop(f'results_unlocked_{election_id}', None)
        session.pop(f'results_unlocked_at_{election_id}', None)
        session.pop(f'results_unlocked_by_{election_id}', None)
        el = database.get_election_by_id(election_id)
        el_title = el['title'] if el else f"Election #{election_id}"
        flash(f"'{el_title}' ballot vault has been securely locked and re-sealed.", "info")
        return redirect(url_for('results', election_id=election_id))
    else:
        for k in list(session.keys()):
            if k.startswith('results_unlocked'):
                session.pop(k, None)
        flash("All election results have been securely locked and sealed.", "info")
        return redirect(url_for('results'))

# ----------------- Live Voting Booth & Biometrics -----------------

@app.route('/booth')
@login_required
def voting_booth():
    """Live Biometric Voting Booth with real-time facial scanner."""
    stats = database.get_election_dashboard_stats()
    active_election = database.get_active_election()
    return render_template('voting_booth.html', stats=stats, election=active_election)

def generate_video_stream():
    """Yields live video frames with pacing, cleanly terminating on client disconnect."""
    camera = face_engine.video_camera
    import time
    try:
        while True:
            frame_bytes = camera.get_frame()
            if frame_bytes:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
                time.sleep(0.04)
            else:
                time.sleep(0.15)
    except (GeneratorExit, ConnectionResetError, BrokenPipeError):
        pass

@app.route('/video_feed')
@login_required
def video_feed():
    return Response(
        generate_video_stream(),
        mimetype='multipart/x-mixed-replace; boundary=frame',
        headers={
            'Cache-Control': 'no-cache, no-store, must-revalidate',
            'Pragma': 'no-cache',
            'Expires': '0'
        }
    )

@app.route('/api/capture_frame')
@login_required
def api_capture_frame():
    """Captures single raw frame for voter registration."""
    camera = face_engine.video_camera
    frame_bytes = camera.get_raw_frame_jpeg()
    if frame_bytes:
        return Response(frame_bytes, mimetype='image/jpeg')
    return jsonify({
        'error': 'Camera hardware switch is currently OFF. Please slide the toggle switch on the RIGHT SIDE of your laptop chassis to turn the webcam ON.'
    }), 503

@app.route('/api/restart_camera', methods=['POST'])
@login_required
def api_restart_camera():
    face_engine.video_camera.stop()
    face_engine.video_camera.start()
    return jsonify({'status': 'restarted', 'is_opened': face_engine.video_camera.is_opened()})

@app.route('/api/latest_booth_event')
@login_required
def api_latest_booth_event():
    return jsonify(face_engine.latest_event)

@app.route('/api/verify_face_upload', methods=['POST'])
@login_required
def api_verify_face_upload():
    """Photo file uploads are strictly barred to maintain voting integrity."""
    return jsonify({
        'success': False,
        'status': 'spoof_detected',
        'message': 'Photo file uploads are strictly prohibited. Voting must be authenticated in person via the live biometric webcam.'
    }), 403

@app.route('/api/scan_booth_frame', methods=['POST'])
@login_required
def api_scan_booth_frame():
    """Biometrically scans a live browser webcam frame from the client."""
    img_bgr = None
    data = request.get_json(silent=True)
    if data and 'image' in data and data['image'].startswith('data:image'):
        try:
            header, encoded = data['image'].split(',', 1)
            img_bytes = base64.b64decode(encoded)
            nparr = np.frombuffer(img_bytes, np.uint8)
            img_bgr = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        except Exception as e:
            return jsonify({'success': False, 'message': str(e)}), 400

    if img_bgr is None:
        return jsonify({'success': False, 'message': 'Live webcam frame required'}), 400

    result = face_engine.verify_voter_by_image(img_bgr, session_id=str(session.get('user_id', 'booth')))
    if result.get('success') and result.get('event', {}).get('status') == 'verified_can_vote':
        voter_internal_id = result['event'].get('voter_internal_id')
        session['booth_verified_voter_id'] = voter_internal_id
        session['booth_verified_at'] = time.time()
        
        # Save snapshot directly to disk (NEVER store large base64 in cookie to prevent >4KB overflow)
        saved_photo_url = None
        try:
            filename = f"live_{voter_internal_id}_{int(time.time())}.jpg"
            save_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'uploads', 'live_votes')
            os.makedirs(save_dir, exist_ok=True)
            file_disk_path = os.path.join(save_dir, filename)
            cv2.imwrite(file_disk_path, img_bgr)
            saved_photo_url = f"/static/uploads/live_votes/{filename}"
            session['booth_verified_live_photo'] = saved_photo_url
        except Exception as e:
            print(f"Error saving live snapshot: {e}")

        # Store in server-side booth memory as a reliable backup
        _last_verified_booth_voter[session.get('user_id', 'booth')] = {
            'id': voter_internal_id,
            'time': time.time(),
            'live_photo': saved_photo_url
        }

    return jsonify(result)

@app.route('/booth/authorize_voter/<int:voter_internal_id>')
@login_required
def authorize_voter(voter_internal_id):
    """Authorizes biometrically verified voter and redirects to the ballot screen."""
    now = time.time()
    verified_id = session.get('booth_verified_voter_id')
    verified_at = session.get('booth_verified_at', 0)

    # Server-side booth memory fallback (immunizes against mobile browser cookie sync drops)
    if verified_id != voter_internal_id:
        cached = _last_verified_booth_voter.get(session.get('user_id', 'booth'))
        if cached and cached.get('id') == voter_internal_id and (now - cached.get('time', 0) < 180):
            verified_id = voter_internal_id
            verified_at = cached.get('time', 0)
            session['booth_verified_voter_id'] = verified_id
            session['booth_verified_at'] = verified_at
            if cached.get('live_photo') and not session.get('booth_verified_live_photo'):
                session['booth_verified_live_photo'] = cached.get('live_photo')

    if verified_id != voter_internal_id or (now - verified_at > 180):
        session.pop('booth_verified_voter_id', None)
        flash("Live facial biometric authentication required at the voting booth before ballot access.", "warning")
        return redirect(url_for('voting_booth'))

    voter = database.get_voter_by_id(voter_internal_id)
    if not voter:
        flash("Voter record not found.", "danger")
        return redirect(url_for('voting_booth'))

    active = database.get_active_election()
    if not active:
        flash("No active election in progress.", "warning")
        return redirect(url_for('voting_booth'))

    if voter.get('election_id') and voter['election_id'] != active['id']:
        flash(f"Access Denied: Elector '{voter['name']}' is registered for a different election and is not in the '{active['title']}' electoral roll.", "danger")
        return redirect(url_for('voting_booth'))

    if voter['has_voted']:
        flash(f"SECURITY ALERT: Voter '{voter['name']}' ({voter['voter_id']}) has already voted! Duplicate vote blocked.", "danger")
        return redirect(url_for('voting_booth'))

    active = database.get_active_election()
    if not active:
        flash("No active election in progress.", "warning")
        return redirect(url_for('voting_booth'))

    session['verified_election_id'] = active['id']
    session['verified_voter_id'] = voter['id']
    session['verified_voter_code'] = voter['voter_id']
    session['verified_voter_name'] = voter['name']
    session['verified_voter_constituency'] = voter['constituency']
    session['verified_live_photo'] = session.get('booth_verified_live_photo')
    session['authorized_at'] = now
    return redirect(url_for('ballot'))

# ----------------- Electronic Ballot Screen -----------------

@app.route('/ballot')
@login_required
def ballot():
    now = time.time()
    voter_id = session.get('verified_voter_id')
    auth_time = session.get('authorized_at') or session.get('booth_verified_at', now)

    if not voter_id or (now - auth_time > 300):
        flash("Biometric verification required! Please face the scanner at the voting booth.", "warning")
        return redirect(url_for('voting_booth'))

    voter = database.get_voter_by_id(voter_id)
    if not voter or voter['has_voted']:
        session.pop('verified_voter_id', None)
        flash("You have already cast your vote! Duplicate voting is prohibited.", "danger")
        return redirect(url_for('voting_booth'))

    active_election = database.get_active_election()
    candidates = database.get_candidates_for_election(active_election['id'] if active_election else None)
    return render_template('ballot.html', voter=voter, election=active_election, candidates=candidates)

@app.route('/cast_vote', methods=['POST'])
@app.route('/api/cast_vote', methods=['POST'])
@login_required
def cast_vote():
    voter_id = session.get('verified_voter_id')
    is_ajax = request.is_json or request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.path.startswith('/api/')

    if not voter_id or time.time() - session.get('booth_verified_at', 0) > 120:
        if is_ajax:
            return jsonify({'success': False, 'message': 'Session expired or unverified voter.'}), 403
        flash("Session expired or biometric verification required.", "warning")
        return redirect(url_for('voting_booth'))

    payload = (request.get_json(silent=True) or {}) if request.is_json else request.form
    candidate_id = payload.get('candidate_id')
    election_id = payload.get('election_id')

    if not candidate_id or not election_id:
        if is_ajax:
            return jsonify({'success': False, 'message': 'Please select a candidate before submitting your vote.'}), 400
        flash("Please select a candidate before submitting your vote.", "danger")
        return redirect(url_for('ballot'))

    try:
        candidate_id, election_id = int(candidate_id), int(election_id)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'message': 'Invalid candidate or election ID.'}), 400
    if election_id != session.get('verified_election_id'):
        return jsonify({'success': False, 'message': 'Election does not match verified ballot.'}), 403

    voter = database.get_voter_by_id(int(voter_id))
    if not voter or (voter.get('election_id') and voter['election_id'] != election_id):
        return jsonify({'success': False, 'message': 'Elector is not registered in this election electoral roll.'}), 403

    # Save live booth presence photo for audit proof and results display
    photo_val = session.pop('verified_live_photo', None) or session.pop('booth_verified_live_photo', None)
    saved_live_photo_path = None
    raw_voter_code = session.get('verified_voter_code', f"V{voter_id}")
    voter_code_str = re.sub(r'[^A-Za-z0-9_\-]', '', str(raw_voter_code))[:32]

    if photo_val and photo_val.startswith('/static/'):
        saved_live_photo_path = photo_val
    elif photo_val and photo_val.startswith('data:image'):
        try:
            _, enc = photo_val.split(',', 1)
            raw = base64.b64decode(enc)
            filename = f"live_{voter_code_str}_{int(time.time())}.jpg"
            save_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'uploads', 'live_votes')
            os.makedirs(save_dir, exist_ok=True)
            file_disk_path = os.path.join(save_dir, filename)
            with open(file_disk_path, 'wb') as f:
                f.write(raw)
            saved_live_photo_path = f"/static/uploads/live_votes/{filename}"
        except Exception as e:
            print(f"Error saving live vote photo from session: {e}")

    # Fallback to hardware camera frame if client b64 not in session
    if not saved_live_photo_path and face_engine.video_camera.is_opened():
        try:
            raw_frame = face_engine.video_camera.get_raw_frame_jpeg()
            if raw_frame:
                filename = f"live_{voter_code_str}_{int(time.time())}.jpg"
                save_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'uploads', 'live_votes')
                os.makedirs(save_dir, exist_ok=True)
                file_disk_path = os.path.join(save_dir, filename)
                with open(file_disk_path, 'wb') as f:
                    f.write(raw_frame)
                saved_live_photo_path = f"/static/uploads/live_votes/{filename}"
        except Exception as e:
            print(f"Error saving fallback live vote photo: {e}")

    success, msg = database.cast_vote(
        voter_internal_id=int(voter_id),
        candidate_id=int(candidate_id),
        election_id=int(election_id),
        verification_method='Facial Biometric',
        live_photo_path=saved_live_photo_path
    )

    if success:
        # Clear voter session so the next voter can authenticate
        cand = database.get_candidate_by_id(int(candidate_id))
        session.pop('verified_voter_id', None)
        session.pop('booth_verified_voter_id', None)
        session.pop('booth_verified_at', None)
        session.pop('verified_election_id', None)
        session.pop('verified_voter_code', None)
        session.pop('verified_voter_name', None)
        session.pop('verified_voter_constituency', None)
        session.pop('verified_live_photo', None)
        session.pop('booth_verified_live_photo', None)
        session['last_voted_candidate'] = cand['name'] if cand else 'Candidate'
        face_engine.reload_enrolled_voters()
        if is_ajax:
            return jsonify({'success': True, 'message': msg, 'redirect': url_for('vote_success')})
        flash(msg, "success")
        return redirect(url_for('vote_success'))
    else:
        if is_ajax:
            return jsonify({'success': False, 'message': msg}), 400
        flash(msg, "danger")
        return redirect(url_for('ballot'))

@app.route('/vote_success')
def vote_success():
    cand_name = session.pop('last_voted_candidate', 'Selected Candidate')
    return render_template('vote_success.html', candidate_name=cand_name)

# ----------------- Voter Roll Management -----------------

@app.route('/voters')
@login_required
def voters_list():
    query = request.args.get('q', '').strip()
    all_elections = database.get_all_elections()
    active_election = database.get_active_election()
    selected_election_id = request.args.get('election_id', type=int)

    target_election_id = selected_election_id or (active_election['id'] if active_election else None)
    current_election = database.get_election_by_id(target_election_id) if target_election_id else active_election

    voters = database.get_all_voters(search_query=query if query else None, election_id=target_election_id)
    return render_template(
        'voters.html',
        voters=voters,
        query=query,
        all_elections=all_elections,
        current_election=current_election,
        selected_election_id=target_election_id
    )

@app.route('/voters/add', methods=['GET', 'POST'])
@login_required
def add_voter():
    all_elections = database.get_all_elections()
    active_election = database.get_active_election()
    selected_election_id = request.args.get('election_id', type=int) or (active_election['id'] if active_election else 1)

    if request.method == 'POST':
        election_id = request.form.get('election_id', type=int) or selected_election_id
        target_election = database.get_election_by_id(election_id) or active_election

        voter_id = re.sub(r'[^A-Za-z0-9_\-]', '', request.form.get('voter_id', '').strip().upper())[:32]
        name = re.sub(r'[<>{}\\\/]', '', request.form.get('name', '').strip())[:60]
        try:
            age = int(request.form.get('age', 18))
            if not 18 <= age <= 120:
                raise ValueError()
        except (ValueError, TypeError):
            flash('Age must be a number between 18 and 120.', 'danger')
            return render_template('add_voter.html', all_elections=all_elections, selected_election_id=election_id, active_election=active_election), 400
        gender = request.form.get('gender', 'Male')
        if gender not in ('Male', 'Female', 'Other'):
            gender = 'Male'
        constituency = re.sub(r'[<>{}\\\/]', '', request.form.get('constituency', '').strip())[:80]
        contact = re.sub(r'[^0-9+\-\s]', '', request.form.get('contact', '').strip())[:20]
        email = re.sub(r'[<>{}\\\/\s]', '', request.form.get('email', '').strip())[:80]

        webcam_data = request.form.get('webcam_image_data', '')
        burst_data_raw = request.form.get('webcam_liveness_burst', '')

        if not voter_id or not name or not constituency:
            flash("Voter ID, Full Name, and Constituency are mandatory.", "danger")
            return render_template('add_voter.html', all_elections=all_elections, selected_election_id=election_id, active_election=active_election)

        # Strictly reject photo files, online images, and require real-time webcam frame
        if request.files.get('photo_file') and request.files.get('photo_file').filename != '':
            flash("Photo file uploads are strictly prohibited. The elector must be present in person facing the live webcam.", "danger")
            return render_template('add_voter.html', all_elections=all_elections, selected_election_id=election_id, active_election=active_election)

        if not webcam_data or not webcam_data.startswith('data:image'):
            flash("Live webcam snapshot is required. Please capture the elector using the live webcam scanner.", "danger")
            return render_template('add_voter.html', all_elections=all_elections, selected_election_id=election_id, active_election=active_election)

        try:
            # Decode burst frames for multi-frame biological motion / blink liveness verification
            burst_frames = []
            if burst_data_raw:
                try:
                    b64_list = json.loads(burst_data_raw)
                    for item in b64_list:
                        if isinstance(item, str) and item.startswith('data:image'):
                            _, enc = item.split(',', 1)
                            b_bytes = base64.b64decode(enc)
                            nparr = np.frombuffer(b_bytes, np.uint8)
                            bf = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                            if bf is not None:
                                burst_frames.append(bf)
                except Exception:
                    pass

            header, encoded = webcam_data.split(',', 1)
            img_bytes = base64.b64decode(encoded)
            nparr = np.frombuffer(img_bytes, np.uint8)
            img_bgr = cv2.imdecode(nparr, cv2.IMREAD_COLOR)

            if img_bgr is None or img_bgr.size == 0:
                flash("Invalid camera frame data received. Please recapture.", "danger")
                return render_template('add_voter.html', all_elections=all_elections, selected_election_id=election_id, active_election=active_election)

            # Enforce multi-frame biological presentation attack detection (rejects Google photos, phone screens, prints)
            if len(burst_frames) >= 2:
                is_live, spoof_reason, liveness_score = face_engine.verify_enrollment_burst(burst_frames)
            else:
                is_live, spoof_reason, liveness_score = face_engine.check_face_liveness(img_bgr, session_id='enrollment')

            if not is_live:
                flash(f"Anti-Spoof Alert: Presentation attack blocked! {spoof_reason}. Online Google photos, phone screens, or static pictures cannot be enrolled. The elector must be physically present.", "danger")
                return render_template('add_voter.html', all_elections=all_elections, selected_election_id=election_id, active_election=active_election)

            encoding, cropped_img, err = face_engine.extract_face_encoding(img_bgr)
            if err or encoding is None:
                flash(f"Biometric extraction error: {err or 'No face detected in live camera frame.'}", "danger")
                return render_template('add_voter.html', all_elections=all_elections, selected_election_id=election_id, active_election=active_election)

            filename = secrets.token_hex(16) + '.jpg'
            save_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            cv2.imwrite(save_path, cropped_img if cropped_img is not None else img_bgr)
            photo_rel_path = f"uploads/voters/{filename}"
            face_encoding = encoding
        except Exception as e:
            flash(f"Error processing facial biometrics: {str(e)}", "danger")
            return render_template('add_voter.html', all_elections=all_elections, selected_election_id=election_id, active_election=active_election)

        v_id, err = database.add_voter(
            voter_id=voter_id,
            name=name,
            age=int(age) if age else 20,
            gender=gender,
            constituency=constituency,
            contact=contact,
            email=email,
            photo_path=photo_rel_path,
            face_encoding=face_encoding,
            election_id=election_id
        )

        if err:
            flash(err, "danger")
            return render_template('add_voter.html', all_elections=all_elections, selected_election_id=election_id, active_election=active_election)

        face_engine.reload_enrolled_voters()
        flash(f"Elector '{name}' ({voter_id}) successfully enrolled into '{target_election['title'] if target_election else 'the election'}' electoral roll!", "success")
        return redirect(url_for('voters_list', election_id=election_id))

    return render_template('add_voter.html', all_elections=all_elections, selected_election_id=selected_election_id, active_election=active_election)

@app.route('/voters/delete/<int:voter_internal_id>', methods=['POST'])
@admin_required
def delete_voter(voter_internal_id):
    if not database.delete_voter(voter_internal_id):
        flash('Cannot remove a voter with recorded votes.', 'danger')
        return redirect(url_for('voters_list'))
    face_engine.reload_enrolled_voters()
    flash("Voter removed from electoral roll.", "info")
    return redirect(url_for('voters_list'))

# ----------------- Elections Hub & Switcher -----------------

@app.route('/elections')
@login_required
def elections_list():
    elections = database.get_all_elections()
    active_election = database.get_active_election()
    return render_template('elections.html', elections=elections, active_election=active_election)

@app.route('/elections/create', methods=['POST'])
@login_required
def create_election():
    if session.get('role') not in ('admin', 'chief_electoral_officer', 'principal', 'college_election_officer'):
        flash("Unauthorized: Only Administrators, Chief Electoral Officers, Principals, and Election Officers can create elections.", "danger")
        return redirect(url_for('elections_list'))

    title = re.sub(r'[<>{}\\\/]', '', request.form.get('title', '').strip())[:100]
    institution_name = re.sub(r'[<>{}\\\/]', '', request.form.get('institution_name', '').strip())[:100]
    election_type = request.form.get('election_type', 'college').strip().lower()
    if election_type not in ('college', 'school', 'general'):
        election_type = 'college'
    description = re.sub(r'[<>{}]', '', request.form.get('description', '').strip())[:500]
    start_date = request.form.get('start_date') or date.today().strftime('%Y-%m-%d')
    end_date = request.form.get('end_date') or date.today().strftime('%Y-%m-%d')
    set_active = bool(request.form.get('set_active'))

    if not title:
        flash("Election title is required.", "danger")
        return redirect(url_for('elections_list'))

    election_id = database.create_election(
        title=title,
        description=description,
        election_type=election_type,
        institution_name=institution_name,
        start_date=start_date,
        end_date=end_date,
        set_active=set_active
    )
    flash(f"Election '{title}' created successfully!", "success")
    return redirect(url_for('elections_list'))

@app.route('/elections/activate/<int:election_id>', methods=['POST'])
@login_required
def activate_election(election_id):
    if session.get('role') not in ('admin', 'chief_electoral_officer', 'principal', 'college_election_officer'):
        flash("Unauthorized: Only Administrators, Chief Electoral Officers, Principals, and Election Officers can activate elections.", "danger")
        return redirect(url_for('elections_list'))

    election = database.get_election_by_id(election_id)
    if not election:
        flash("Election record not found.", "danger")
        return redirect(url_for('elections_list'))

    database.set_active_election(election_id)
    face_engine.reload_enrolled_voters()
    flash(f"'{election['title']}' is now the ACTIVE election for all voting booths and EVM ballots! Booth biometric registry updated.", "success")
    return redirect(url_for('elections_list'))

@app.route('/elections/delete/<int:election_id>', methods=['POST'])
@admin_required
def delete_election(election_id):
    success, msg = database.delete_election(election_id)
    if not success:
        flash(msg or "Cannot delete this election.", "danger")
    else:
        flash("Election removed successfully.", "info")
    return redirect(url_for('elections_list'))

# ----------------- Candidates & Election Admin -----------------

@app.route('/candidates', methods=['GET', 'POST'])
@login_required
def candidates_list():
    all_elections = database.get_all_elections()
    active_election = database.get_active_election()
    
    selected_id = request.args.get('election_id', type=int)
    if not selected_id:
        selected_id = active_election['id'] if active_election else (all_elections[0]['id'] if all_elections else 1)
    current_election = database.get_election_by_id(selected_id) or active_election

    if request.method == 'POST':
        if session.get('role') not in ('admin', 'chief_electoral_officer', 'principal', 'college_election_officer'):
            flash("Unauthorized: Only Administrators, Chief Electoral Officers, Principals, and Election Officers can submit candidate nominations.", "danger")
            return redirect(url_for('candidates_list', election_id=selected_id))

        target_election_id = request.form.get('election_id', type=int) or selected_id
        name = re.sub(r'[<>{}\\\/]', '', request.form.get('name', '').strip())[:60]
        party_name = re.sub(r'[<>{}\\\/]', '', request.form.get('party_name', '').strip())[:60]
        position = re.sub(r'[<>{}\\\/]', '', request.form.get('position', 'President').strip())[:60]
        manifesto = re.sub(r'[<>{}]', '', request.form.get('manifesto', '').strip())[:500]

        if not name or not party_name:
            flash("Candidate name and Party/Alliance name are required.", "danger")
        else:
            database.add_candidate(
                election_id=target_election_id,
                name=name,
                party_name=party_name,
                manifesto=manifesto,
                position=position
            )
            flash(f"Candidate '{name}' nominated for {position} successfully!", "success")
            return redirect(url_for('candidates_list', election_id=target_election_id))

    candidates = database.get_candidates_for_election(selected_id)
    return render_template('candidates.html', candidates=candidates, election=current_election, all_elections=all_elections, selected_id=selected_id)

@app.route('/candidates/delete/<int:candidate_id>', methods=['POST'])
@admin_required
def delete_candidate(candidate_id):
    if not database.delete_candidate(candidate_id):
        flash('Cannot remove a candidate with recorded votes.', 'danger')
        return redirect(url_for('candidates_list'))
    flash("Candidate nomination removed.", "info")
    return redirect(url_for('candidates_list'))

# ----------------- Audit Logs & Export -----------------

@app.route('/audit')
@login_required
def audit_logs():
    all_elections = database.get_all_elections()
    active_election = database.get_active_election()
    selected_id = request.args.get('election_id', type=int)
    show_all = request.args.get('show_all') == '1'

    target_id = None if show_all else (selected_id or (active_election['id'] if active_election else None))
    current_election = database.get_election_by_id(target_id) if target_id else None

    stats = database.get_election_dashboard_stats(election_id=target_id)
    votes = database.get_all_votes_audit_trail(election_id=target_id)
    integrity_report = database.verify_database_integrity()
    return render_template(
        'audit_logs.html',
        votes=votes,
        stats=stats,
        integrity_report=integrity_report,
        all_elections=all_elections,
        current_election=current_election,
        selected_id=target_id,
        show_all=show_all
    )

@app.route('/api/database_integrity')
@login_required
def api_database_integrity():
    report = database.verify_database_integrity()
    return jsonify(report)

@app.route('/audit/export')
@admin_required
def export_audit_csv():
    selected_id = request.args.get('election_id', type=int)
    votes = database.get_all_votes_audit_trail(election_id=selected_id)
    df = pd.DataFrame(votes)
    export_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data')
    el = database.get_election_by_id(selected_id) if selected_id else None
    el_suffix = f"_{el['election_type']}_{el['id']}" if el else "_all"
    filename = f"Election_Audit_Log{el_suffix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    csv_path = os.path.join(export_dir, filename)
    df.to_csv(csv_path, index=False)
    return send_file(csv_path, as_attachment=True, download_name=filename, mimetype='text/csv')

@app.route('/admin/reset_votes', methods=['POST'])
@login_required
def reset_votes():
    if session.get('role') not in ('admin', 'chief_electoral_officer'):
        flash("Only Chief Election Commissioner or Chief Electoral Officer can reset poll counts.", "danger")
        return redirect(url_for('dashboard'))

    target_id = request.form.get('election_id', type=int)
    el = database.get_election_by_id(target_id) if target_id else None
    if el:
        database.reset_all_votes(election_id=target_id)
        face_engine.reload_enrolled_voters()
        flash(f"Ballots for '{el['title']}' have been safely reset. Other elections remain untouched.", "info")
        return redirect(url_for('audit_logs', election_id=target_id))
    else:
        database.reset_all_votes()
        face_engine.reload_enrolled_voters()
        flash("All election votes and ballot locks have been reset for a fresh demonstration.", "warning")
        return redirect(url_for('audit_logs'))

# ----------------- Main Launcher -----------------

if __name__ == '__main__':
    print("=" * 65)
    print("  VOTESURE - SMART FACE RECOGNITION BIOMETRIC E-VOTING SYSTEM")
    print("  Official Public E-Voting Commission")
    print("  Server launching on: http://127.0.0.1:5000")
    print("  Default Admin Login: username='admin', password='admin123'")
    print("  Default Officer Login: username='officer', password='officer123'")
    print("=" * 65)
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
