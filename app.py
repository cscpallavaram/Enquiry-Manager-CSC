"""
Student Enquiry Manager - Local Flask Application
A simple web app for importing, managing, and assigning student enquiry calls.
"""

import os
import csv
import json
import io
import logging
from datetime import datetime
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, session, send_file, jsonify
)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

import whatsapp as wa

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = "change-this-in-production-enquiry-manager-2024"
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///enquiry_manager.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = os.path.join(os.path.dirname(__file__), "uploads")
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

db = SQLAlchemy(app)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("enquiry_manager")

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    enquiries = db.relationship("Enquiry", backref="assignee", lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class ColumnDef(db.Model):
    """Defines the flexible columns for enquiries."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)  # internal key
    label = db.Column(db.String(150), nullable=False)              # display name
    data_type = db.Column(db.String(20), default="text")           # text, number, date, email, phone
    order_index = db.Column(db.Integer, default=0)
    is_visible = db.Column(db.Boolean, default=True)
    is_required = db.Column(db.Boolean, default=False)

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "label": self.label,
            "data_type": self.data_type,
            "order_index": self.order_index,
            "is_visible": self.is_visible,
            "is_required": self.is_required,
        }


class Enquiry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    data = db.Column(db.Text, default="{}")  # JSON string of flexible fields
    assigned_to = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    status = db.Column(db.String(30), default="new")  # new, in_progress, contacted, closed
    notes = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def get_data(self):
        try:
            return json.loads(self.data) if self.data else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    def set_data(self, d):
        self.data = json.dumps(d, ensure_ascii=False)

    def get_value(self, key, default=""):
        return self.get_data().get(key, default)


class MessageLog(db.Model):
    """Tracks outbound WhatsApp messages and delivery status updates."""
    id = db.Column(db.Integer, primary_key=True)
    enquiry_id = db.Column(db.Integer, db.ForeignKey("enquiry.id"), nullable=True)
    phone = db.Column(db.String(20), nullable=False)
    direction = db.Column(db.String(10), default="out")  # out | in
    template_name = db.Column(db.String(100), default="")
    body_preview = db.Column(db.String(255), default="")
    wa_message_id = db.Column(db.String(120), nullable=True, index=True)
    status = db.Column(db.String(30), default="pending")  # pending, sent, delivered, read, failed
    error = db.Column(db.Text, default="")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    enquiry = db.relationship("Enquiry", backref=db.backref("messages", lazy=True))


def _log_whatsapp_result(enquiry_id, phone, template_name, result, body_preview=""):
    """Persist send attempt. Never raises — logging must not break the request."""
    try:
        entry = MessageLog(
            enquiry_id=enquiry_id,
            phone=phone or "",
            direction="out",
            template_name=template_name or "",
            body_preview=(body_preview or "")[:255],
            wa_message_id=result.get("message_id"),
            status="sent" if result.get("ok") else "failed",
            error=(result.get("error") or "")[:500],
        )
        db.session.add(entry)
        db.session.commit()
    except Exception as exc:
        logger.exception("Failed to write MessageLog: %s", exc)
        try:
            db.session.rollback()
        except Exception:
            pass


def _safe_notify_new(enquiry):
    """Fire-and-forget new-enquiry WhatsApp. Never blocks/raises to caller."""
    if not wa.is_configured():
        return
    try:
        result = wa.notify_new_enquiry(enquiry)
        phone = wa.get_enquiry_phone(enquiry) or ""
        _log_whatsapp_result(
            enquiry.id, phone, wa.TEMPLATE_NEW_ENQUIRY, result, "New enquiry acknowledgement"
        )
        if not result.get("ok"):
            logger.warning("WhatsApp new-enquiry notify failed: %s", result.get("error"))
    except Exception as exc:
        logger.exception("WhatsApp new-enquiry notify error: %s", exc)


def _safe_notify_status(enquiry, old_status=None):
    """Fire-and-forget status-update WhatsApp. Never blocks/raises to caller."""
    if not wa.is_configured():
        return
    try:
        result = wa.notify_status_update(enquiry, old_status=old_status)
        phone = wa.get_enquiry_phone(enquiry) or ""
        _log_whatsapp_result(
            enquiry.id, phone, wa.TEMPLATE_STATUS_UPDATE, result,
            f"Status → {enquiry.status}",
        )
        if not result.get("ok"):
            logger.warning("WhatsApp status notify failed: %s", result.get("error"))
    except Exception as exc:
        logger.exception("WhatsApp status notify error: %s", exc)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login"))
        user = User.query.get(session["user_id"])
        if not user or not user.is_admin:
            flash("Admin access required.", "danger")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated


def current_user():
    if "user_id" in session:
        return User.query.get(session["user_id"])
    return None


# ---------------------------------------------------------------------------
# Routes – Auth
# ---------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username, is_active=True).first()
        if user and user.check_password(password):
            session["user_id"] = user.id
            session["username"] = user.username
            session["is_admin"] = user.is_admin
            flash(f"Welcome, {user.username}!", "success")
            return redirect(url_for("dashboard"))
        flash("Invalid username or password.", "danger")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Routes – Dashboard
# ---------------------------------------------------------------------------
@app.route("/")
@login_required
def dashboard():
    user = current_user()
    total = Enquiry.query.count()
    unassigned = Enquiry.query.filter_by(assigned_to=None).count()
    my_enquiries = Enquiry.query.filter_by(assigned_to=user.id).count() if user else 0
    by_status = (
        db.session.query(Enquiry.status, db.func.count(Enquiry.id))
        .group_by(Enquiry.status)
        .all()
    )
    status_counts = {s: c for s, c in by_status}

    recent = (
        Enquiry.query.order_by(Enquiry.updated_at.desc()).limit(8).all()
    )
    columns = ColumnDef.query.filter_by(is_visible=True).order_by(ColumnDef.order_index).all()

    return render_template(
        "dashboard.html",
        total=total,
        unassigned=unassigned,
        my_enquiries=my_enquiries,
        status_counts=status_counts,
        recent=recent,
        columns=columns,
        user=user,
    )


# ---------------------------------------------------------------------------
# Routes – Enquiries
# ---------------------------------------------------------------------------
@app.route("/enquiries")
@login_required
def enquiries():
    status_filter = request.args.get("status", "")
    assignee_filter = request.args.get("assignee", "")
    search = request.args.get("q", "").strip()

    query = Enquiry.query

    if status_filter:
        query = query.filter_by(status=status_filter)
    if assignee_filter == "me":
        query = query.filter_by(assigned_to=session["user_id"])
    elif assignee_filter == "unassigned":
        query = query.filter_by(assigned_to=None)
    elif assignee_filter.isdigit():
        query = query.filter_by(assigned_to=int(assignee_filter))

    enquiries_list = query.order_by(Enquiry.created_at.desc()).all()

    # Simple search across JSON data
    if search:
        search_lower = search.lower()
        filtered = []
        for e in enquiries_list:
            data_str = json.dumps(e.get_data()).lower()
            if search_lower in data_str or (e.notes and search_lower in e.notes.lower()):
                filtered.append(e)
        enquiries_list = filtered

    columns = ColumnDef.query.filter_by(is_visible=True).order_by(ColumnDef.order_index).all()
    # Staff only for assignee filter (admins are not assignees)
    users = (
        User.query.filter_by(is_active=True, is_admin=False)
        .order_by(User.username)
        .all()
    )

    return render_template(
        "enquiries.html",
        enquiries=enquiries_list,
        columns=columns,
        users=users,
        status_filter=status_filter,
        assignee_filter=assignee_filter,
        search=search,
    )


@app.route("/enquiries/<int:enquiry_id>", methods=["GET", "POST"])
@login_required
def enquiry_detail(enquiry_id):
    enquiry = Enquiry.query.get_or_404(enquiry_id)
    columns = ColumnDef.query.order_by(ColumnDef.order_index).all()
    # Only non-admin staff appear in the assignment dropdown
    users = (
        User.query.filter_by(is_active=True, is_admin=False)
        .order_by(User.username)
        .all()
    )

    if request.method == "POST":
        action = request.form.get("action")
        if action == "update":
            old_status = enquiry.status
            data = enquiry.get_data()
            for col in columns:
                val = request.form.get(f"field_{col.name}", "")
                data[col.name] = val
            enquiry.set_data(data)
            enquiry.status = request.form.get("status", enquiry.status)
            enquiry.notes = request.form.get("notes", "")
            assignee = request.form.get("assigned_to")
            if assignee:
                target = User.query.get(int(assignee))
                if target and target.is_admin:
                    flash("Cannot assign enquiries to admin users.", "warning")
                    return redirect(url_for("enquiry_detail", enquiry_id=enquiry.id))
            enquiry.assigned_to = int(assignee) if assignee else None
            enquiry.updated_at = datetime.utcnow()
            db.session.commit()
            # Notify on status change only (non-blocking)
            if enquiry.status != old_status:
                _safe_notify_status(enquiry, old_status=old_status)
            flash("Enquiry updated successfully.", "success")
            return redirect(url_for("enquiry_detail", enquiry_id=enquiry.id))
        elif action == "send_whatsapp":
            # Manual send from detail page
            if not wa.is_configured():
                flash("WhatsApp is not configured. Set WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID.", "warning")
            else:
                phone = wa.get_enquiry_phone(enquiry)
                if not phone:
                    flash("No valid phone number found on this enquiry.", "warning")
                else:
                    result = wa.notify_status_update(enquiry)
                    _log_whatsapp_result(
                        enquiry.id, phone, wa.TEMPLATE_STATUS_UPDATE, result, "Manual send"
                    )
                    if result.get("ok"):
                        flash("WhatsApp message sent.", "success")
                    else:
                        flash(f"WhatsApp send failed: {result.get('error')}", "danger")
            return redirect(url_for("enquiry_detail", enquiry_id=enquiry.id))
        elif action == "delete":
            db.session.delete(enquiry)
            db.session.commit()
            flash("Enquiry deleted.", "info")
            return redirect(url_for("enquiries"))

    message_logs = (
        MessageLog.query.filter_by(enquiry_id=enquiry.id)
        .order_by(MessageLog.created_at.desc())
        .limit(20)
        .all()
    )
    return render_template(
        "enquiry_detail.html",
        enquiry=enquiry,
        columns=columns,
        users=users,
        message_logs=message_logs,
        whatsapp_configured=wa.is_configured(),
    )


@app.route("/enquiries/new", methods=["GET", "POST"])
@login_required
def enquiry_new():
    columns = ColumnDef.query.order_by(ColumnDef.order_index).all()
    # Only non-admin staff appear in the assignment dropdown
    users = (
        User.query.filter_by(is_active=True, is_admin=False)
        .order_by(User.username)
        .all()
    )

    if request.method == "POST":
        data = {}
        for col in columns:
            data[col.name] = request.form.get(f"field_{col.name}", "")
        enquiry = Enquiry(
            data=json.dumps(data, ensure_ascii=False),
            status=request.form.get("status", "new"),
            notes=request.form.get("notes", ""),
        )
        assignee = request.form.get("assigned_to")
        if assignee:
            target = User.query.get(int(assignee))
            if target and target.is_admin:
                flash("Cannot assign enquiries to admin users.", "warning")
                return redirect(url_for("enquiry_new"))
            enquiry.assigned_to = int(assignee)
        db.session.add(enquiry)
        db.session.commit()
        # Acknowledgement WhatsApp (non-blocking; failures are logged only)
        _safe_notify_new(enquiry)
        flash("New enquiry created.", "success")
        return redirect(url_for("enquiry_detail", enquiry_id=enquiry.id))

    return render_template("enquiry_form.html", columns=columns, users=users, enquiry=None)


# ---------------------------------------------------------------------------
# Routes – Assignment
# ---------------------------------------------------------------------------
@app.route("/assign", methods=["GET", "POST"])
@login_required
def assign():
    # Staff users only (non-admin) — admins monitor/assign but do not receive work
    staff_users = (
        User.query.filter_by(is_active=True, is_admin=False)
        .order_by(User.username)
        .all()
    )
    all_active = User.query.filter_by(is_active=True).order_by(User.username).all()
    unassigned = Enquiry.query.filter_by(assigned_to=None).order_by(Enquiry.created_at).all()
    columns = ColumnDef.query.filter_by(is_visible=True).order_by(ColumnDef.order_index).limit(4).all()

    if request.method == "POST":
        action = request.form.get("action")
        if action == "auto_assign":
            # Round-robin among active non-admin staff only
            if not staff_users:
                flash("No active staff users to assign to. Create non-admin users first.", "warning")
                return redirect(url_for("assign"))
            if not unassigned:
                flash("No unassigned enquiries.", "info")
                return redirect(url_for("assign"))

            for i, enq in enumerate(unassigned):
                enq.assigned_to = staff_users[i % len(staff_users)].id
                enq.updated_at = datetime.utcnow()
            db.session.commit()
            flash(
                f"Auto-assigned {len(unassigned)} enquiries across {len(staff_users)} staff users.",
                "success",
            )
            return redirect(url_for("assign"))

        elif action == "manual":
            enquiry_id = request.form.get("enquiry_id")
            user_id = request.form.get("user_id")
            enq = Enquiry.query.get(enquiry_id)
            if enq:
                # Block assigning to admin accounts
                if user_id:
                    target = User.query.get(int(user_id))
                    if target and target.is_admin:
                        flash("Cannot assign enquiries to admin users. Admins only monitor and assign.", "warning")
                        return redirect(url_for("assign"))
                enq.assigned_to = int(user_id) if user_id else None
                enq.updated_at = datetime.utcnow()
                db.session.commit()
                flash("Assignment updated.", "success")
            return redirect(url_for("assign"))

    # Workload summary — show staff only (admins are not assignees)
    workload = []
    for u in staff_users:
        count = Enquiry.query.filter_by(assigned_to=u.id).count()
        workload.append({"user": u, "count": count})

    return render_template(
        "assign.html",
        unassigned=unassigned,
        users=staff_users,  # dropdowns only list staff
        columns=columns,
        workload=workload,
    )


# ---------------------------------------------------------------------------
# Routes – CSV Import
# ---------------------------------------------------------------------------
@app.route("/import", methods=["GET", "POST"])
@login_required
def import_csv():
    if request.method == "POST":
        if "file" not in request.files:
            flash("No file selected.", "warning")
            return redirect(url_for("import_csv"))
        file = request.files["file"]
        if file.filename == "":
            flash("No file selected.", "warning")
            return redirect(url_for("import_csv"))
        if not file.filename.lower().endswith(".csv"):
            flash("Please upload a CSV file.", "danger")
            return redirect(url_for("import_csv"))

        try:
            # Read CSV
            stream = io.StringIO(file.stream.read().decode("utf-8-sig"))
            reader = csv.DictReader(stream)
            if not reader.fieldnames:
                flash("CSV appears empty or has no headers.", "danger")
                return redirect(url_for("import_csv"))

            # Normalize headers → column names
            headers = [h.strip() for h in reader.fieldnames if h and h.strip()]
            existing_cols = {c.name: c for c in ColumnDef.query.all()}
            max_order = db.session.query(db.func.max(ColumnDef.order_index)).scalar() or 0

            # Create missing columns
            for i, header in enumerate(headers):
                # Simple slug for internal name
                name = header.lower().replace(" ", "_").replace("-", "_")
                name = "".join(c for c in name if c.isalnum() or c == "_")
                if not name:
                    name = f"col_{i}"
                # Ensure unique
                base = name
                counter = 1
                while name in existing_cols or ColumnDef.query.filter_by(name=name).first():
                    name = f"{base}_{counter}"
                    counter += 1

                if name not in existing_cols:
                    max_order += 1
                    col = ColumnDef(
                        name=name,
                        label=header,
                        data_type="text",
                        order_index=max_order,
                        is_visible=True,
                    )
                    db.session.add(col)
                    existing_cols[name] = col

            db.session.commit()

            # Re-fetch columns mapped by original header
            col_map = {}
            for header in headers:
                name = header.lower().replace(" ", "_").replace("-", "_")
                name = "".join(c for c in name if c.isalnum() or c == "_")
                # Find matching column (best effort)
                col = ColumnDef.query.filter(
                    db.or_(
                        ColumnDef.label == header,
                        ColumnDef.name == name,
                    )
                ).first()
                if col:
                    col_map[header] = col.name

            # Insert rows
            count = 0
            for row in reader:
                data = {}
                for header, value in row.items():
                    if header and header in col_map:
                        data[col_map[header]] = (value or "").strip()
                enquiry = Enquiry(data=json.dumps(data, ensure_ascii=False), status="new")
                db.session.add(enquiry)
                count += 1

            db.session.commit()
            flash(f"Successfully imported {count} enquiries. You can now edit columns if needed.", "success")
            return redirect(url_for("columns"))
        except Exception as e:
            db.session.rollback()
            flash(f"Error importing CSV: {str(e)}", "danger")
            return redirect(url_for("import_csv"))

    return render_template("import.html")


# ---------------------------------------------------------------------------
# Routes – Column Management
# ---------------------------------------------------------------------------
@app.route("/columns", methods=["GET", "POST"])
@login_required
def columns():
    if request.method == "POST":
        action = request.form.get("action")

        if action == "add":
            label = request.form.get("label", "").strip()
            name = request.form.get("name", "").strip().lower().replace(" ", "_")
            name = "".join(c for c in name if c.isalnum() or c == "_") or "new_field"
            data_type = request.form.get("data_type", "text")
            if ColumnDef.query.filter_by(name=name).first():
                flash(f"Column name '{name}' already exists.", "warning")
            else:
                max_order = db.session.query(db.func.max(ColumnDef.order_index)).scalar() or 0
                col = ColumnDef(
                    name=name,
                    label=label or name.replace("_", " ").title(),
                    data_type=data_type,
                    order_index=max_order + 1,
                )
                db.session.add(col)
                db.session.commit()
                flash("Column added.", "success")

        elif action == "update":
            col_id = request.form.get("col_id")
            col = ColumnDef.query.get(col_id)
            if col:
                col.label = request.form.get("label", col.label).strip()
                col.data_type = request.form.get("data_type", col.data_type)
                col.is_visible = request.form.get("is_visible") == "on"
                col.is_required = request.form.get("is_required") == "on"
                try:
                    col.order_index = int(request.form.get("order_index", col.order_index))
                except ValueError:
                    pass
                db.session.commit()
                flash("Column updated.", "success")

        elif action == "delete":
            col_id = request.form.get("col_id")
            col = ColumnDef.query.get(col_id)
            if col:
                # Optionally clean data from enquiries (keep for simplicity)
                db.session.delete(col)
                db.session.commit()
                flash(f"Column '{col.label}' deleted.", "info")

        elif action == "rename":
            col_id = request.form.get("col_id")
            new_name = request.form.get("new_name", "").strip().lower().replace(" ", "_")
            new_name = "".join(c for c in new_name if c.isalnum() or c == "_")
            col = ColumnDef.query.get(col_id)
            if col and new_name and new_name != col.name:
                if ColumnDef.query.filter_by(name=new_name).first():
                    flash("That internal name is already taken.", "warning")
                else:
                    # Migrate data keys in all enquiries
                    old_name = col.name
                    for enq in Enquiry.query.all():
                        data = enq.get_data()
                        if old_name in data:
                            data[new_name] = data.pop(old_name)
                            enq.set_data(data)
                    col.name = new_name
                    db.session.commit()
                    flash("Column renamed and data migrated.", "success")

        return redirect(url_for("columns"))

    cols = ColumnDef.query.order_by(ColumnDef.order_index).all()
    return render_template("columns.html", columns=cols)


# ---------------------------------------------------------------------------
# Routes – User Management
# ---------------------------------------------------------------------------
@app.route("/users", methods=["GET", "POST"])
@admin_required
def users():
    if request.method == "POST":
        action = request.form.get("action")

        if action == "create":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            is_admin = request.form.get("is_admin") == "on"
            if not username or not password:
                flash("Username and password are required.", "warning")
            elif User.query.filter_by(username=username).first():
                flash("Username already exists.", "warning")
            else:
                user = User(username=username, is_admin=is_admin)
                user.set_password(password)
                db.session.add(user)
                db.session.commit()
                flash(f"User '{username}' created.", "success")

        elif action == "toggle_active":
            user_id = request.form.get("user_id")
            user = User.query.get(user_id)
            if user and user.id != session["user_id"]:
                user.is_active = not user.is_active
                db.session.commit()
                flash(f"User '{user.username}' {'activated' if user.is_active else 'deactivated'}.", "info")
            else:
                flash("Cannot deactivate yourself.", "warning")

        elif action == "reset_password":
            user_id = request.form.get("user_id")
            new_pass = request.form.get("new_password", "")
            user = User.query.get(user_id)
            if user and new_pass:
                user.set_password(new_pass)
                db.session.commit()
                flash(f"Password reset for '{user.username}'.", "success")

        elif action == "delete":
            user_id = request.form.get("user_id")
            user = User.query.get(user_id)
            if user and user.id != session["user_id"]:
                # Unassign their enquiries
                Enquiry.query.filter_by(assigned_to=user.id).update({"assigned_to": None})
                db.session.delete(user)
                db.session.commit()
                flash(f"User '{user.username}' deleted.", "info")
            else:
                flash("Cannot delete yourself.", "warning")

        return redirect(url_for("users"))

    all_users = User.query.order_by(User.username).all()
    return render_template("users.html", users=all_users)


# ---------------------------------------------------------------------------
# WhatsApp webhook (public — no login)
# Meta calls GET to verify, POST for delivery status / inbound messages
# ---------------------------------------------------------------------------
@app.route("/webhook/whatsapp", methods=["GET", "POST"])
def whatsapp_webhook():
    if request.method == "GET":
        # Subscription verification
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        if mode == "subscribe" and token == wa.WHATSAPP_VERIFY_TOKEN:
            logger.info("WhatsApp webhook verified")
            return challenge or "", 200
        logger.warning("WhatsApp webhook verification failed")
        return "Forbidden", 403

    # POST — status updates or inbound messages
    try:
        payload = request.get_json(force=True, silent=True) or {}
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                # Delivery / read status
                for st in value.get("statuses", []):
                    wa_id = st.get("id")
                    status = st.get("status")  # sent, delivered, read, failed
                    if not wa_id:
                        continue
                    log = MessageLog.query.filter_by(wa_message_id=wa_id).first()
                    if log:
                        log.status = status or log.status
                        if st.get("errors"):
                            try:
                                log.error = str(st["errors"][0].get("message", ""))[:500]
                            except (IndexError, TypeError, AttributeError):
                                pass
                        log.updated_at = datetime.utcnow()
                        db.session.commit()
                        logger.info("WhatsApp status %s → %s", wa_id, status)
                # Optional: log inbound messages (user replies) for audit
                for msg in value.get("messages", []):
                    from_phone = msg.get("from", "")
                    text_body = ""
                    if msg.get("type") == "text":
                        text_body = (msg.get("text") or {}).get("body", "")
                    entry_log = MessageLog(
                        enquiry_id=None,
                        phone=from_phone,
                        direction="in",
                        template_name="",
                        body_preview=text_body[:255],
                        wa_message_id=msg.get("id"),
                        status="received",
                    )
                    db.session.add(entry_log)
                    db.session.commit()
    except Exception as exc:
        logger.exception("Webhook processing error: %s", exc)
        # Always return 200 so Meta does not retry aggressively for app bugs
    return jsonify({"status": "ok"}), 200


# ---------------------------------------------------------------------------
# Init DB & default admin
# ---------------------------------------------------------------------------
def init_db():
    with app.app_context():
        db.create_all()
        # Create default admin if none exists
        if not User.query.filter_by(username="admin").first():
            admin = User(username="admin", is_admin=True)
            admin.set_password("admin123")
            db.session.add(admin)
            db.session.commit()
            print("Created default admin user: admin / admin123")


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    init_db()
    print("=" * 60)
    print("  Student Enquiry Manager")
    print("  Open your browser at: http://127.0.0.1:5000")
    print("  Default login: admin / admin123")
    print("=" * 60)
    app.run(debug=True, host="192.168.1.6", port=5015)
