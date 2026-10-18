# Student Enquiry Manager

A clean, local web application for managing student enquiry / call records.

**Features**
- CSV import with automatic column creation
- Edit columns after import (add, rename, change type, hide, delete)
- Assign enquiries to users (manual or auto-distribute)
- User accounts with login (admin + regular users)
- Simple dashboard and list views designed for non-technical staff
- WhatsApp Business Cloud API integration (acknowledgement + status templates)

Built with **Flask** + **SQLite**. Minimal dependencies. Runs entirely on your machine.

---

## Quick Start

### 1. Requirements
- Python 3.8 or newer

### 2. Install dependencies

```bash
cd enquiry_manager
pip install -r requirements.txt
```

### 3. Run the server

```bash
python app.py
```

Open your browser at: **http://127.0.0.1:5000**

### 4. Default login
| Username | Password  | Role  |
|----------|-----------|-------|
| admin    | admin123  | Admin |

**Change the admin password after first login** (Users → Reset password).

---

## How to Use

### Import a CSV
1. Go to **Import CSV** in the sidebar.
2. Choose a `.csv` file whose first row contains column headers (e.g. `Name,Phone,Email,Course`).
3. Click **Import**. New columns are created automatically from the headers.
4. You are taken to the **Columns** page so you can adjust labels, types, or visibility.

### Edit columns
1. Open **Columns**.
2. Change the **Label** (what staff see), **Type** (text / number / date / email / phone), visibility, or order.
3. Click the checkmark to save each row.
4. Use **Add Column** to create extra fields.
5. Use **Rename Internal Name** if you need to change the underlying key (data is migrated).

### Assign enquiries
1. Go to **Assign**.
2. Unassigned records appear in a table.
3. **Auto-Assign All** – distributes them evenly (round-robin) across active **staff** users only.
4. Or pick a user from the dropdown next to any single enquiry and click **Go**.
5. **Admin users are never assigned enquiries.** Admins only import, monitor, assign work, and view staff progress.

### Manage users (Admin only)
1. Open **Users**.
2. Create new accounts, set admin flag, reset passwords, activate/deactivate, or delete users.
3. Deleting a user unassigns their enquiries.

### Work with enquiries
- **Dashboard** – overview counts and recent items.
- **Enquiries** – searchable, filterable list (by status or assignee).
- Click any enquiry to edit fields, status, notes, and assignment.
- **New Enquiry** – add a record manually.

---

## Project Structure

```
enquiry_manager/
├── app.py              # Main application (models + routes)
├── whatsapp.py         # Meta WhatsApp Cloud API helper
├── requirements.txt
├── README.md
├── templates/          # HTML pages
└── (uploads/ and enquiry_manager.db created at runtime)
```

---

## WhatsApp Business API integration

Uses **Meta WhatsApp Cloud API**. Business-initiated messages must use **approved templates**.

### Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `WHATSAPP_TOKEN` | Yes | Permanent system user access token |
| `WHATSAPP_PHONE_NUMBER_ID` | Yes | Phone number ID from Meta app |
| `WHATSAPP_VERIFY_TOKEN` | Yes* | Shared secret for webhook verification (any strong string you choose) |
| `WHATSAPP_API_VERSION` | No | Default `v21.0` |
| `WHATSAPP_DEFAULT_COUNTRY` | No | Default `91` (India) for 10-digit local numbers |
| `WHATSAPP_TEMPLATE_NEW` | No | Template name for new enquiry ack (default `enquiry_ack`) |
| `WHATSAPP_TEMPLATE_STATUS` | No | Template name for status updates (default `enquiry_status_update`) |

\* Required if you enable the webhook for delivery tracking.

Example (Linux/macOS):

```bash
export WHATSAPP_TOKEN="your_permanent_token"
export WHATSAPP_PHONE_NUMBER_ID="123456789012345"
export WHATSAPP_VERIFY_TOKEN="enquiry_verify_token_change_me"
export WHATSAPP_DEFAULT_COUNTRY="91"
python app.py
```

### When messages are sent

| Event | Behaviour |
|-------|-----------|
| New enquiry created | Sends `enquiry_ack` template (params: name, enquiry id) |
| Status changed on detail page | Sends `enquiry_status_update` template (name, status, id) |
| Manual **Send Status Message** button | Same status template |
| API / network failure | Logged to `MessageLog`; enquiry save still succeeds |

Phone numbers are read from enquiry JSON fields named like `phone`, `mobile`, `mobi`, etc., and normalized to international digits (e.g. `919876543210`).

### Meta Developer Console setup

1. Go to [developers.facebook.com](https://developers.facebook.com/) → create or open an app → add **WhatsApp**.
2. Under **API Setup**, copy **Phone number ID** and generate a **permanent** access token (System User recommended for production).
3. **Message templates**: create and submit for approval, e.g.:
   - `enquiry_ack` — body: `Hello {{1}}, we received your enquiry #{{2}}. Our team will contact you soon.`
   - `enquiry_status_update` — body: `Hello {{1}}, your enquiry #{{3}} status is now: {{2}}.`
4. **Webhook**:
   - Callback URL: `https://YOUR_PUBLIC_HOST/webhook/whatsapp`  
     (use [ngrok](https://ngrok.com/) for local testing: `ngrok http 5000`)
   - Verify token: same value as `WHATSAPP_VERIFY_TOKEN`
   - Subscribe to **messages** (includes status updates)
5. For testing before go-live, add recipient numbers under **API Setup → To**.

### Webhook endpoint

- `GET /webhook/whatsapp` — Meta verification challenge  
- `POST /webhook/whatsapp` — delivery statuses (`sent` / `delivered` / `read` / `failed`) and optional inbound messages  

Status updates are written to the `message_log` table and shown on the enquiry detail page.

---

## Notes
- Database file `enquiry_manager.db` is created automatically in the same folder.
- The default secret key is fine for local use; change it if you ever expose the app beyond localhost.
- Only active **non-admin** users receive enquiry assignments.
- Column data is stored as JSON, so the schema stays flexible.
- WhatsApp sends never block creating or updating an enquiry; failures are logged only.
