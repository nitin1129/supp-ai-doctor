# SuppAI Doctors API

Backend for the SuppAI doctor flow: a doctor applies, an admin reviews and accepts, the doctor signs in and runs appointments, and a member books and tracks a consult. It implements *SuppAI Doctors Flow v1 (29 Sep 2026)*, plus the gaps found while reviewing that doc (listed below).

Stack: FastAPI · MongoDB (pymongo) · Razorpay · argon2 · JWT. Works on a standalone mongod, a replica set or Atlas; no multi-document transactions are needed.

## Run it

```bash
cp .env.example .env            # fill SUPPAI_JWT_SECRET and SUPPAI_DATA_ENCRYPTION_KEY
docker compose up --build       # MongoDB + API on :8000 + worker
open http://localhost:8000/docs # interactive API docs
```

Without Docker:

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload   # creates the indexes on startup (or: python -m app.models)
python -m app.worker            # separate terminal
```

Tests use an in-memory MongoDB (mongomock) by default, so they need no server:

```bash
pytest
SUPPAI_MONGO_URL=mongodb://localhost:27017 pytest   # against a real MongoDB (uses the suppai_test database)
```

## How it connects to the rest of suppai.in

| Piece | How |
|---|---|
| Admin and member auth | Issued by the main SuppAI auth service. Bearer JWT signed with `SUPPAI_JWT_SECRET`, claims `{"sub": "<user id>", "role": "admin" \| "member"}`. |
| Doctor auth | Issued here at `/auth/doctor/login`. Claims include `doctor_id` and a token version. Doctor endpoints only ever read `doctor_id` from the token. |
| Member contact details | Notifications to members are queued with recipient `member:<id>`. Resolve that to phone/email inside `app/worker.py → deliver_message`. |
| Email / SMS / WhatsApp | `deliver_message` logs by default. Plug in SES/SendGrid and MSG91/Gupshup there. |
| Video | Jitsi room per appointment (no API key needed). For Daily or Twilio, implement `app/services/video.py`. |
| Documents | Private local storage with 5-minute signed links. To move to S3, keep the three functions in `app/services/storage.py` and return a presigned URL. |
| Payments | `SUPPAI_PAYMENTS_PROVIDER=razorpay`. Point a Razorpay webhook at `/payments/razorpay/webhook` for `payment.captured`, `order.paid`, `refund.processed` and `refund.failed`. |

## API

| Endpoint | Purpose |
|---|---|
| **Doctor applies** (`/join/doctor`) | |
| `POST /doctors/applications` | Start a draft; returns `resume_token` once |
| `GET · PATCH /doctors/applications/{id}` | Resume / save a step (`X-Application-Token` header) |
| `POST · DELETE /doctors/applications/{id}/documents` | Upload or remove a document (PDF/JPEG/PNG, 10 MB) |
| `POST /doctors/applications/{id}/email/send-code` | Send a 6-digit OTP to the application email |
| `POST /doctors/applications/{id}/email/verify` | Verify the OTP (`{"code": "123456"}`); required before submit |
| `POST /doctors/applications/{id}/submit` | Submit or resubmit after a hold; lists anything missing |
| **Admin → Onboarding → Doctors** | |
| `GET /admin/doctors/applications?status=&q=` | List and search applications |
| `GET /admin/doctors/applications/{id}` | Full answers and signed document links (view is audited) |
| `POST …/{id}/review` | Mark reviewing |
| `POST …/{id}/hold` · `…/reject` | Needs a note for the doctor |
| `POST …/{id}/accept` | Create doctor and login in one transaction; emails a set-password link |
| `POST /admin/doctors/{id}/resend-invite` | The "New password" button: fresh link, old one revoked |
| `POST /admin/doctors/{id}/suspend` · `reactivate` | Suspension signs the doctor out and cancels upcoming consults with a full refund |
| `GET /admin/doctors/profile-changes` · `POST …/{id}` | Approve or reject fee and specialty changes |
| `GET /admin/appointments?status=needs_review` · `POST …/{id}/resolve` | Consults the doctor never marked |
| **Doctor sign-in** | |
| `POST /auth/doctor/login` | Username or email + password; locks for 15 min after 5 failures |
| `POST /auth/doctor/set-password` | From the invite or reset link (24 h, single use) |
| `POST /auth/doctor/forgot-password` | Always 202 |
| `POST /auth/doctor/otp/request` | Send a sign-in code; always 202 (no email enumeration) |
| `POST /auth/doctor/otp/verify` | Exchange code `{"email","code"}` → bearer token |
| **Member auth** | |
| `POST /auth/member/otp/request` | Send a sign-in / sign-up code; always 202 |
| `POST /auth/member/otp/verify` | Exchange code `{"email","code","name?"}` → member bearer token; creates account if `MEMBER_OTP_SIGNUP=true` |
| **Doctor dashboard** (`/doctor`) | |
| `GET · PUT /doctor/me` | Profile; fee and specialty changes go to admin |
| `GET · PUT /doctor/hours` | Weekly windows (several per day), slot length, notice |
| `GET · POST /doctor/leave` · `DELETE /doctor/leave/{id}` | Days off with a patient-facing note; "Take today off" is today's date |
| `GET /doctor/appointments?date=&days=` | Today (`days=1`), next 7 days (`days=7`), calendar |
| `PATCH /doctor/appointments/{id}/status` | `completed` or `no_show` |
| **Member** | |
| `GET /doctors?specialty=&pincode=&mode=` | Find a doctor; cards include next availability, rating, experience and fee |
| `GET /doctors/{id}/slots?date=` | One day's slots; booked ones come back `available: false` |
| `POST /appointments` | Hold the slot 5 min and get a Razorpay order |
| `GET /appointments` · `GET /appointments/{id}` | Track → Consults |
| `PATCH /appointments/{id}/reschedule` | Until 4 h before |
| `POST /appointments/{id}/cancel` | Full refund until 4 h before, none after |
| `POST /appointments/{id}/review` | 1–5 stars after a completed consult |
| `POST /payments/razorpay/webhook` | Marks paid; idempotent |

## How MongoDB is used

| Collection | Holds |
|---|---|
| `doctor_applications` | One per application. The seven steps are in `data`; uploads are embedded in `documents`. |
| `doctors` | Approved doctors. Weekly hours are embedded in `hours` as IST minutes (`{weekday, start_min, end_min}`). |
| `doctor_users`, `password_tokens` | Doctor logins and one-time set-password links (only hashes are stored). |
| `doctor_leave` | Days off, unique per doctor and date. |
| `appointments` | Bookings, with a doctor snapshot in `doctor` and the timeline embedded in `events`. |
| `payouts`, `reviews`, `profile_change_requests` | One payout and one review per appointment (unique indexes). |
| `payment_webhook_events`, `audit_log`, `outbox`, `counters` | Webhook idempotency, admin audit, notification queue, SA-DOC numbering. |
| `otp_codes` | 6-digit HMAC-SHA256 hashed codes (plain code never stored); TTL 1 h. |
| `otp_verify_attempts` | Per-IP verify attempt counters; TTL 1 h. |

Consistency without transactions:
- Every status change is a conditional update on the current status (`find_one_and_update({_id, status: …})`), so two requests can't both move the same record.
- Accepting an application claims it first. If any later step fails, the doctor and login it created are removed again.
- `appointments.holds_slot` is kept in step with the status and drives the unique slot index. `models.status_fields()` sets both together.
- The outbox entry is written right after the change it belongs to, and jobs re-check the appointment before acting.

## Statuses

**Application:** `draft → submitted → under_review → accepted`, with `on_hold` (the doctor edits and resubmits under the same SA-DOC reference) and `rejected`.

**Appointment:** one row is read by both screens.

| Stored `status` | Member sees | Backend also does |
|---|---|---|
| `pending_payment` | awaiting_payment | Slot held for 5 min |
| `expired` | (hidden) | Hold lapsed; slot free |
| `booked` | upcoming → in_progress from 10 min before | Notify doctor; reminders 24 h and 1 h before; video room 10 min before |
| `completed` | completed | Ask for a review; queue the payout minus commission |
| `no_show` | no_show | Patient didn't join: no refund by default; doctor is paid |
| `cancelled` | cancelled | Refund per the rules; free the slot |
| `needs_review` | completed | Doctor didn't mark within 24 h; admin resolves |

## Decisions made beyond the flow doc

These fill gaps in the v1 doc. All the numbers are settings in `.env`.

- **Email OTP.** Set `SUPPAI_EMAIL_PROVIDER=brevo` and `SUPPAI_BREVO_API_KEY=…` for production. In dev (`log`, the default) every OTP is printed to the server log — grep for `"is your SuppAI code"` or just watch the terminal. OTP rules: 6 digits, 10-minute TTL, 5 max attempts (code is then burned), 1 resend per 60 s per email+purpose, 5 per email per hour, 20 per IP per hour, 10 verify attempts per IP per 15 min.
- **Application email must be verified before submit.** `POST …/email/send-code` then `POST …/email/verify`. If the email is later changed in a PATCH, verification resets.
- **No passwords are shown or emailed.** Accept emails a single-use, 24 h set-password link. The "temporary password" panel in the admin UI should show the username and invite status instead.
- **Usernames** are `dr.first.last`. If that's taken, a number is added (`dr.leela.menon2`).
- **Aadhaar** is checksum-validated, then only the last 4 digits are kept. PAN and bank account are Fernet-encrypted at rest. The admin sees the full PAN, and each view is audited.
- **Accept requires `registration_verified: true`**, meaning the admin has checked the number on the NMC or state council register.
- **"In progress" is derived from time** (10 min before start until end), so nobody has to trigger it.
- **No-show** means the patient didn't join: no refund (`PATIENT_NO_SHOW_REFUND_PERCENT=0`) and the doctor is paid. A doctor no-show is handled by admin through `resolve` with `doctor_no_show`, which gives a full refund and no payout.
- **Day off with bookings** returns 409 with the affected consults. The doctor confirms with `cancel_booked: true`, which cancels them with a full refund and sends the doctor's note to the members.
- **Late payments:** if Razorpay confirms after the hold has lapsed and the slot is still free, the booking goes through. If the slot is taken, the payment is refunded automatically.
- **Commission is 15%** (`COMMISSION_PERCENT`). This is a placeholder; set your real rate.
- **Double booking** is blocked twice: a short per-doctor lease while a booking is checked and written, plus a unique index on `(doctor_id, start_at)` limited to appointments with `holds_slot: true`.
- **Times** are stored in UTC and returned with an IST label. Weekly hours are IST wall-clock times.

## Not built yet

- E-prescriptions and consultation notes (expected by the Telemedicine Practice Guidelines 2020) and patient consent capture before the consult.
- RazorpayX payout webhooks. The worker sends queued payouts (`queued → sending → sent | failed`, one idempotency key per payout; `SUPPAI_PAYOUTS_PROVIDER=razorpayx`), but final settlement (`payout.processed` / `reversed`) is not tracked yet. The RazorpayX adapter has not been run against the live API.
- Real email, SMS and WhatsApp senders, and the production member contact source (`PlatformContactSource` in `app/services/contacts.py`; dev/tests use the in-memory source).
- Rate limits (slowapi, per client IP) are on application create/submit, doctor login and forgot-password; settings `SUPPAI_RATE_LIMIT_*`. Counters are per process unless `SUPPAI_RATE_LIMIT_STORAGE_URI` points at a shared store.
