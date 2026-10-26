# Database-connected update

Start with **DATABASE_SETUP.md** for the current instructions, fixes, tests and limitations. Your existing SQLite data is retained. Do not run seed_data.py on your real project data. The older project description below contains unverified security and biometric claims; this is a local academic prototype.

# SMART FACE RECOGNITION BIOMETRIC E-VOTING SYSTEM

**Department of Computer Science & Engineering**  
**Tontadarya College of Engineering (TCE), Gadag**

---

## 📌 Executive Summary
The **Smart Face Recognition Biometric E-Voting System** is an automated, secure, and tamper-resistant digital voting solution developed to ensure maximum integrity, efficiency, and transparency in student elections, council elections, and institutional polls. 

Traditional voting systems (manual paper ballots and ordinary digital kiosks) suffer from critical vulnerabilities:
1. **Voter Impersonation & Proxy Voting:** Unauthorized individuals casting votes on behalf of absent electors.
2. **Duplicate Voting:** Ineffective ballot tracking allowing individuals to attempt multiple votes.
3. **Slow & Error-Prone Counting:** Manual tallying of ballots is tedious, delayed, and susceptible to counting errors.
4. **Lack of Transparent Audit Trails:** Paper registers are difficult to reconcile securely.

To solve these problems, this system integrates **Computer Vision and Deep Facial Biometrics (128-dimensional encodings)** with an **Electronic Voting Machine (EVM) ballot interface** and an **immutable relational database**. Electors are biometrically authenticated at the live camera kiosk before the electronic ballot unlocks. The moment a ballot is cast, the elector's status is permanently locked (`has_voted = 1`) and any subsequent attempts are immediately flagged and blocked.

---

## 🏗️ System Architecture & Workflow

```
       +-------------------------------------------------------+
       |             Camera Video Input (Kiosk)                |
       |       Real-Time DirectShow (CAP_DSHOW) Stream         |
       +-------------------------------------------------------+
                                   |
                                   v
       +-------------------------------------------------------+
       |             Face Detection Module                     |
       |           (Haar Cascade / HOG Detector)               |
       +-------------------------------------------------------+
                                   |
                                   v
       +-------------------------------------------------------+
       |            Face Recognition Engine                    |
       |         (128-d Deep Metric Embedding)                 |
       +-------------------------------------------------------+
                                   |
                                   v
       +-------------------------------------------------------+
       |           Electoral Roll Matching & Verification      |
       |        Compares with Stored Biometrics in SQLite      |
       +-------------------------------------------------------+
                                   |
              +--------------------+--------------------+
              |                                         |
     [Match Found: Verified]                      [Unknown Face]
              |                                         |
              v                                         v
+-------------------------------+             +-----------------------+
|  Duplicate Vote Check         |             | Red Bounding Box      |
|  (Is has_voted == 1?)         |             | "UNKNOWN - NOT IN     |
+-------------------------------+             |  ELECTORAL ROLL"      |
     |                     |                  +-----------------------+
  [YES]                  [NO]
     |                     |
     v                     v
+-----------------------+  +---------------------------------------+
| 🚫 ACCESS BLOCKED     |  | ✅ ACCESS GRANTED                     |
| Bright Red HUD Alert  |  | Green HUD Bounding Box                |
| "DUPLICATE VOTE       |  | Unlocks Secure EVM Ballot Screen      |
|  BLOCKED"             |  +---------------------------------------+
+-----------------------+                      |
                                               v
                                   +---------------------------------------+
                                   |       Electronic Ballot (EVM)         |
                                   | - Contesting Candidates & Slates      |
                                   | - One-Click Ballot Submission         |
                                   | - EVM Beep Tone Confirmation          |
                                   +---------------------------------------+
                                               |
                                               v
                                   +---------------------------------------+
                                   |      Atomic Database Transaction      |
                                   | - votes table: INSERT record          |
                                   | - voters table: SET has_voted = 1     |
                                   | - candidates table: votes_count + 1   |
                                   +---------------------------------------+
                                               |
                                               v
                                   +---------------------------------------+
                                   |    Real-Time Audit Trail & Analytics  |
                                   | - Live Turnout & Vote Share Charts    |
                                   | - CSV Audit Export for Scrutiny       |
                                   +---------------------------------------+
```

---

## 🚀 Key Modules & Capabilities

### 1. 📷 Live Biometric Voting Booth (`/booth`)
- Continuous live camera kiosk powered by OpenCV DirectShow (`cv2.CAP_DSHOW`) for zero-lag streaming on Windows.
- Real-time facial bounding boxes with color-coded status banners:
  - 🟢 **Green (Eligible):** Identified registered elector; single-click "Authorize Ballot" button enabled.
  - 🔴 **Red (Blocked):** Elector has already voted. Bright security alert: *"DUPLICATE VOTE BLOCKED - Voter has already cast their ballot."*
  - 🟡 **Yellow (Unrecognized):** Face detected but not registered in the official electoral roll.
- Live telemetry panel reporting detected face coordinates, verification confidence, and electoral roll matching.

### 2. 🗳️ Electronic Voting Machine (EVM) Ballot (`/ballot`)
- Modern digital EVM interface listing all contesting candidates, party slates, and manifestos.
- Secure confirmation modal before ballot submission to prevent accidental clicks.
- Authentic EVM confirmation buzzer tone upon successful vote recording using Web Audio API.
- Instant session cleanup and automatic redirect back to the voting kiosk for the next elector.

### 3. 👥 Electoral Roll Directory & Biometric Enrollment (`/voters`, `/voters/add`)
- Comprehensive registry of electors with Voter ID (EPIC), demographic data, and voting status.
- Single-click biometric enrollment using the live webcam stream (`/api/capture_frame`) or photo upload.
- Automated extraction and serialization of 128-dimensional facial encodings using Python `pickle`.

### 4. 🏅 Contesting Candidates Management (`/candidates`)
- Dynamic ballot slate management for active elections.
- Add and withdraw candidate nominations with real-time vote count tracking.

### 5. 📊 Live Election Analytics & Standings (`/results`, `/dashboard`)
- Real-time voter turnout progress bar and key performance indicators.
- Interactive **Chart.js** visualizations:
  - Candidate-wise vote distribution bar chart.
  - Overall vote share percentage donut chart.
- Dynamic front-runner banner identifying the leading candidate and margin.

### 6. 🛡️ Tamper-Proof Audit Trail & CSV Export (`/audit`)
- Complete cryptographic record of all cast votes with timestamp and biometric verification method.
- SQLite `UNIQUE(election_id, voter_id)` database constraint enforcing the strict *"One Person, One Vote"* standard at the database engine level.
- Downloadable audit log in CSV format for official scrutiny and election certification.

---

## 🛠️ Technology Stack

| Layer | Technologies |
|---|---|
| **Programming Language** | Python 3.11+ |
| **Computer Vision & AI** | OpenCV (`cv2`), `face_recognition`, `dlib`, `numpy` |
| **Web Application Framework** | Flask, Jinja2, Werkzeug |
| **Database** | SQLite3 with WAL mode and foreign key constraints |
| **Frontend UI & Styling** | HTML5, Bootstrap 5.3, Bootstrap Icons, Custom CSS |
| **Data Analytics & Charts** | Chart.js 4.4, Pandas |
| **Audio Confirmation** | HTML5 Web Audio API synthesizer |

---

## 💻 Installation & Quickstart Guide

### Prerequisites
- Python 3.10 or 3.11 installed on Windows / Linux / macOS.
- Built-in or external USB webcam.

### 1. Clone / Open Repository
```bash
cd c:\Users\savan\OneDrive\Pictures\Desktop\thiranex
```

### 2. Install Required Dependencies
```bash
pip install -r requirements.txt
```

### 3. Initialize & Seed Database (Optional / Already Initialized)
```bash
python seed_data.py
```
*Pre-populates the database with an active election ("TCE GADAG - Student Council General Election 2026"), 4 candidates, and registered electors.*

### 4. Launch the Web Application
```bash
python app.py
```
Open your browser and navigate to:
```
http://127.0.0.1:5000
```

---

## 🔐 Default Access Credentials

| Role | Username | Password | Access Privileges |
|---|---|---|---|
| **Chief Election Commissioner (Admin)** | `admin` | `admin123` | Full portal access, voter enrollment, candidate nominations, audit export, demo reset |
| **Polling Booth Officer** | `officer` | `officer123` | Voting booth kiosk, voter roll inspection, live turnout view |

---

## 🧪 Demonstration Walkthrough

1. **Log in** with username `admin` and password `admin123`.
2. Go to **Voting Booth** (`/booth`) to view the live biometric facial scanner.
3. When an enrolled elector appears in front of the camera, the system highlights their face in **green** and identifies their Voter ID.
4. Click **Authorize Ballot** to open the **Electronic Ballot Screen** (`/ballot`).
5. Select a candidate and click **Confirm & Submit Vote**. Hear the authentic EVM tone and see the success screen.
6. Return to the **Voting Booth** with the same voter: the system immediately flags a **bright red duplicate alert** and locks the ballot!
7. Check **Live Results** (`/results`) and **Audit Trail** (`/audit`) to inspect updated charts and immutable records.

---

**Developed for Dept. of Computer Science & Engineering, TCE Gadag.**
