import os
import cv2
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from PIL import Image, ImageTk
from datetime import datetime, date
import database
import face_engine

class AttendanceDesktopApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Face Recognition Attendance System - Dept. of CSE, TCE Gadag")
        self.root.geometry("1080x720")
        self.root.minsize(960, 640)
        self.root.configure(bg="#f8fafc")

        database.init_db()
        face_engine.reload_enrolled_faces()

        self.cap = None
        self.is_camera_running = False
        self.current_frame = None

        self._create_header()
        self._create_notebook()
        
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _create_header(self):
        header_frame = tk.Frame(self.root, bg="#0f172a", height=64)
        header_frame.pack(fill=tk.X, side=tk.TOP)

        title_lbl = tk.Label(
            header_frame,
            text="TONTADARYA COLLEGE OF ENGINEERING, GADAG",
            font=("Arial", 14, "bold"),
            fg="#ffffff",
            bg="#0f172a"
        )
        title_lbl.pack(anchor="w", padx=20, pady=(8, 0))

        sub_lbl = tk.Label(
            header_frame,
            text="Dept. of Computer Science & Engineering • AI Face Recognition Attendance System",
            font=("Arial", 9),
            fg="#94a3b8",
            bg="#0f172a"
        )
        sub_lbl.pack(anchor="w", padx=20, pady=(0, 8))

    def _create_notebook(self):
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=15, pady=15)

        # Tab 1: Live Attendance
        self.tab_kiosk = tk.Frame(self.notebook, bg="#ffffff")
        self.notebook.add(self.tab_kiosk, text="  Live Camera Attendance  ")
        self._build_kiosk_tab()

        # Tab 2: Students Directory
        self.tab_students = tk.Frame(self.notebook, bg="#ffffff")
        self.notebook.add(self.tab_students, text="  Student Management  ")
        self._build_students_tab()

        # Tab 3: Attendance Records
        self.tab_records = tk.Frame(self.notebook, bg="#ffffff")
        self.notebook.add(self.tab_records, text="  Attendance Records  ")
        self._build_records_tab()

    # ---------------- TAB 1: KIOSK ----------------
    def _build_kiosk_tab(self):
        left_frame = tk.Frame(self.tab_kiosk, bg="#000000", width=640, height=480)
        left_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.video_label = tk.Label(left_frame, bg="#0f172a", text="Camera Inactive\nClick 'Start Camera' below", font=("Arial", 12), fg="#94a3b8")
        self.video_label.pack(fill=tk.BOTH, expand=True)

        ctrl_frame = tk.Frame(self.tab_kiosk, bg="#ffffff", width=340)
        ctrl_frame.pack(side=tk.RIGHT, fill=tk.BOTH, padx=10, pady=10)

        tk.Label(ctrl_frame, text="Attendance Control Panel", font=("Arial", 12, "bold"), bg="#ffffff", fg="#0f172a").pack(anchor="w", pady=(0, 10))

        btn_box = tk.Frame(ctrl_frame, bg="#ffffff")
        btn_box.pack(fill=tk.X, pady=5)

        self.btn_toggle_cam = tk.Button(
            btn_box,
            text="▶ Start Live Camera",
            font=("Arial", 10, "bold"),
            bg="#2563eb",
            fg="#ffffff",
            padx=12,
            pady=6,
            relief=tk.FLAT,
            command=self.toggle_camera
        )
        self.btn_toggle_cam.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)

        self.lbl_status = tk.Label(ctrl_frame, text="Scanner Ready", font=("Arial", 10, "bold"), bg="#e2e8f0", fg="#475569", pady=6)
        self.lbl_status.pack(fill=tk.X, pady=10)

        tk.Label(ctrl_frame, text="Today's Marked Attendance:", font=("Arial", 10, "bold"), bg="#ffffff", fg="#334155").pack(anchor="w", pady=(10, 5))

        # List of today's attendance
        self.kiosk_tree = ttk.Treeview(ctrl_frame, columns=("Time", "Roll", "Name"), show="headings", height=15)
        self.kiosk_tree.heading("Time", text="Time")
        self.kiosk_tree.heading("Roll", text="Roll No")
        self.kiosk_tree.heading("Name", text="Student Name")
        self.kiosk_tree.column("Time", width=70)
        self.kiosk_tree.column("Roll", width=80)
        self.kiosk_tree.column("Name", width=120)
        self.kiosk_tree.pack(fill=tk.BOTH, expand=True)

        self.refresh_kiosk_list()

    def toggle_camera(self):
        if not self.is_camera_running:
            self.cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
            if not self.cap.isOpened():
                self.cap = cv2.VideoCapture(0)
            if not self.cap.isOpened():
                messagebox.showerror("Camera Error", "Could not access system webcam.")
                return
            self.is_camera_running = True
            self.btn_toggle_cam.config(text="⏹ Stop Camera", bg="#dc2626")
            self.update_video_stream()
        else:
            self.is_camera_running = False
            if self.cap:
                self.cap.release()
                self.cap = None
            self.btn_toggle_cam.config(text="▶ Start Live Camera", bg="#2563eb")
            self.video_label.config(image="", text="Camera Stopped")

    def update_video_stream(self):
        if not self.is_camera_running or self.cap is None:
            return

        ret, frame = self.cap.read()
        if ret and frame is not None:
            self.current_frame = frame.copy()
            # Feed through face_engine frame generator or draw recognition
            h, w, _ = frame.shape
            
            # Simple scaling and RGB conversion
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(rgb)
            img = img.resize((600, 450), Image.ANTIALIAS if hasattr(Image, 'ANTIALIAS') else Image.LANCZOS)
            imgtk = ImageTk.PhotoImage(image=img)
            self.video_label.imgtk = imgtk
            self.video_label.configure(image=imgtk)

        self.root.after(30, self.update_video_stream)

    def refresh_kiosk_list(self):
        for item in self.kiosk_tree.get_children():
            self.kiosk_tree.delete(item)
        records = database.get_today_records()
        for r in records:
            self.kiosk_tree.insert("", tk.END, values=(r['time'], r['roll_number'], r['name']))

    # ---------------- TAB 2: STUDENTS ----------------
    def _build_students_tab(self):
        container = tk.Frame(self.tab_students, bg="#ffffff")
        container.pack(fill=tk.BOTH, expand=True, padx=15, pady=15)

        # Left: Student List
        left_box = tk.Frame(container, bg="#ffffff")
        left_box.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10))

        tk.Label(left_box, text="Enrolled Students Directory", font=("Arial", 11, "bold"), bg="#ffffff").pack(anchor="w", pady=(0, 8))

        self.stud_tree = ttk.Treeview(left_box, columns=("ID", "Name", "Roll", "Class", "Biometrics"), show="headings")
        self.stud_tree.heading("ID", text="ID")
        self.stud_tree.heading("Name", text="Name")
        self.stud_tree.heading("Roll", text="Roll Number")
        self.stud_tree.heading("Class", text="Class")
        self.stud_tree.heading("Biometrics", text="Face Model")
        self.stud_tree.column("ID", width=40)
        self.stud_tree.column("Name", width=140)
        self.stud_tree.column("Roll", width=90)
        self.stud_tree.column("Class", width=70)
        self.stud_tree.column("Biometrics", width=90)
        self.stud_tree.pack(fill=tk.BOTH, expand=True)

        btn_row = tk.Frame(left_box, bg="#ffffff")
        btn_row.pack(fill=tk.X, pady=8)

        tk.Button(btn_row, text="Refresh List", command=self.refresh_students_list).pack(side=tk.LEFT, padx=2)
        tk.Button(btn_row, text="Delete Selected", bg="#fee2e2", fg="#991b1b", command=self.delete_selected_student).pack(side=tk.LEFT, padx=5)

        # Right: Add Student Form
        right_box = tk.LabelFrame(container, text=" Enroll New Student ", bg="#ffffff", font=("Arial", 10, "bold"), padx=15, pady=15)
        right_box.pack(side=tk.RIGHT, fill=tk.Y, padx=(10, 0))

        tk.Label(right_box, text="Full Name:", bg="#ffffff").pack(anchor="w")
        self.ent_name = tk.Entry(right_box, width=28)
        self.ent_name.pack(fill=tk.X, pady=(0, 8))

        tk.Label(right_box, text="Roll / USN:", bg="#ffffff").pack(anchor="w")
        self.ent_roll = tk.Entry(right_box, width=28)
        self.ent_roll.pack(fill=tk.X, pady=(0, 8))

        tk.Label(right_box, text="Class & Section:", bg="#ffffff").pack(anchor="w")
        self.cmb_class = ttk.Combobox(right_box, values=["CSE-6A", "CSE-6B", "CSE-8A", "CSE-8B", "CSE-4A", "CSE-4B"])
        self.cmb_class.current(0)
        self.cmb_class.pack(fill=tk.X, pady=(0, 8))

        tk.Label(right_box, text="Contact Phone:", bg="#ffffff").pack(anchor="w")
        self.ent_phone = tk.Entry(right_box, width=28)
        self.ent_phone.pack(fill=tk.X, pady=(0, 8))

        tk.Label(right_box, text="Email:", bg="#ffffff").pack(anchor="w")
        self.ent_email = tk.Entry(right_box, width=28)
        self.ent_email.pack(fill=tk.X, pady=(0, 15))

        tk.Button(
            right_box,
            text="Capture Face & Save",
            bg="#16a34a",
            fg="#ffffff",
            font=("Arial", 10, "bold"),
            pady=6,
            command=self.save_new_student
        ).pack(fill=tk.X)

        self.refresh_students_list()

    def refresh_students_list(self):
        for item in self.stud_tree.get_children():
            self.stud_tree.delete(item)
        students = database.get_all_students()
        for s in students:
            bio_status = "Enrolled" if s['has_face_encoding'] else "Missing"
            self.stud_tree.insert("", tk.END, values=(s['id'], s['name'], s['roll_number'], s['class_section'], bio_status))

    def save_new_student(self):
        name = self.ent_name.get().strip()
        roll = self.ent_roll.get().strip().upper()
        section = self.cmb_class.get().strip()
        phone = self.ent_phone.get().strip()
        email = self.ent_email.get().strip()

        if not name or not roll or not section:
            messagebox.showwarning("Incomplete Form", "Name, Roll Number, and Class are required.")
            return

        face_enc = None
        photo_rel = None

        if self.current_frame is not None:
            enc, crop_img, err = face_engine.extract_face_encoding(self.current_frame)
            if not err:
                face_enc = enc
                filename = f"{roll}_{int(time.time())}.jpg"
                save_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'uploads', 'students')
                os.makedirs(save_dir, exist_ok=True)
                save_path = os.path.join(save_dir, filename)
                cv2.imwrite(save_path, crop_img if crop_img is not None else self.current_frame)
                photo_rel = f"uploads/students/{filename}"

        student_id, err = database.add_student(
            name=name,
            roll_number=roll,
            class_section=section,
            contact=phone,
            email=email,
            photo_path=photo_rel,
            face_encoding=face_enc
        )

        if err:
            messagebox.showerror("Error", err)
        else:
            face_engine.reload_enrolled_faces()
            messagebox.showinfo("Success", f"Student {name} registered successfully!")
            self.ent_name.delete(0, tk.END)
            self.ent_roll.delete(0, tk.END)
            self.ent_phone.delete(0, tk.END)
            self.ent_email.delete(0, tk.END)
            self.refresh_students_list()

    def delete_selected_student(self):
        selected = self.stud_tree.selection()
        if not selected:
            messagebox.showwarning("Select Student", "Please select a student from the list.")
            return
        item = self.stud_tree.item(selected[0])
        student_id = item['values'][0]
        if messagebox.askyesno("Confirm Deletion", f"Delete student #{student_id} and all their attendance records?"):
            database.delete_student(student_id)
            face_engine.reload_enrolled_faces()
            self.refresh_students_list()

    # ---------------- TAB 3: RECORDS ----------------
    def _build_records_tab(self):
        container = tk.Frame(self.tab_records, bg="#ffffff")
        container.pack(fill=tk.BOTH, expand=True, padx=15, pady=15)

        toolbar = tk.Frame(container, bg="#ffffff")
        toolbar.pack(fill=tk.X, pady=(0, 10))

        tk.Button(toolbar, text="Refresh Logs", command=self.refresh_records_tab).pack(side=tk.LEFT, padx=2)
        tk.Button(toolbar, text="Export CSV Report", bg="#2563eb", fg="#ffffff", command=self.export_csv_report).pack(side=tk.LEFT, padx=5)

        self.rec_tree = ttk.Treeview(container, columns=("ID", "Date", "Time", "Roll", "Name", "Class", "Method", "Status"), show="headings")
        for col in ("ID", "Date", "Time", "Roll", "Name", "Class", "Method", "Status"):
            self.rec_tree.heading(col, text=col)
        self.rec_tree.column("ID", width=40)
        self.rec_tree.column("Date", width=90)
        self.rec_tree.column("Time", width=80)
        self.rec_tree.column("Roll", width=90)
        self.rec_tree.column("Name", width=140)
        self.rec_tree.column("Class", width=70)
        self.rec_tree.column("Method", width=120)
        self.rec_tree.column("Status", width=80)
        self.rec_tree.pack(fill=tk.BOTH, expand=True)

        self.refresh_records_tab()

    def refresh_records_tab(self):
        for item in self.rec_tree.get_children():
            self.rec_tree.delete(item)
        records = database.get_attendance_records()
        for r in records:
            self.rec_tree.insert("", tk.END, values=(
                r['id'], r['date'], r['time'], r['roll_number'], r['name'], r['class_section'], r['verified_method'], r['status']
            ))

    def export_csv_report(self):
        save_file = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV Files", "*.csv")],
            initialfile=f"Attendance_Report_{date.today().strftime('%Y%m%d')}.csv"
        )
        if save_file:
            import pandas as pd
            records = database.get_attendance_records()
            df = pd.DataFrame(records)
            df.to_csv(save_file, index=False)
            messagebox.showinfo("Export Successful", f"Report saved to:\n{save_file}")

    def on_close(self):
        self.is_camera_running = False
        if self.cap:
            self.cap.release()
        self.root.destroy()

if __name__ == '__main__':
    root = tk.Tk()
    app = AttendanceDesktopApp(root)
    root.mainloop()
