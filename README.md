# MBA Ethics Flask App

Integrated Flask application for the MBA Capstone workflow and the Ethics workflow.

## Features

- MBA routes under `/mba`
- Ethics routes under `/ethics`
- Shared authentication and POPIA confirmation
- Microsoft Entra ID sign-in support through Authlib
- PostgreSQL persistence through SQLAlchemy/Alembic
- Database-backed storage for uploaded and generated documents
- Email notifications for workflow invitations, approvals, and follow-ups

## Local Setup

Create a local environment file from the example:

```powershell
Copy-Item .env.example .env
```

Install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Set `DATABASE_URL` in `.env` to your own local PostgreSQL connection string. Do not commit `.env`.

Initialize or update the database:

```powershell
flask --app run.py db upgrade
flask --app run.py sync-db
```

Create starter admin accounts:

```powershell
flask --app run.py create-admins
```

The command prints generated temporary passwords for newly created accounts. Store them securely and change them before real use.

Create MBA staff users as needed:

```powershell
flask --app run.py create-mba-staff --email supervisor@example.com --password "<temporary-password>" --role scholar --scholar-role supervisor --first-name "Jane" --last-name "Supervisor"
flask --app run.py create-mba-staff --email examiner@example.com --password "<temporary-password>" --role examiner --first-name "John" --last-name "Examiner"
```

Run the app:

```powershell
flask --app run.py run
```

Open `http://localhost:5000`.

## Deployment

Install dependencies from `requirements.txt`, then start the production server with:

```bash
gunicorn run:app
```

## Environment Variables

Required for production:

```text
SECRET_KEY=
DATABASE_URL=
PUBLIC_BASE_URL=
```

Optional Microsoft login:

```text
MICROSOFT_CLIENT_ID=
MICROSOFT_CLIENT_SECRET=
MICROSOFT_TENANT_ID=common
MICROSOFT_REDIRECT_URI=
```

Optional SMTP delivery:

```text
MAIL_SERVER=
MAIL_PORT=587
MAIL_USE_TLS=true
MAIL_USE_SSL=false
MAIL_TIMEOUT=20
MAIL_USERNAME=
MAIL_PASSWORD=
MAIL_DEFAULT_SENDER=
MAIL_LOGO_URL=
```

Render Free web services cannot send outbound traffic on SMTP ports `25`, `465`, or `587`.
For Gmail SMTP on Render, use a paid Render instance type, or switch production email to an email provider that supports an HTTPS API or a non-blocked SMTP submission port.
Set `PUBLIC_BASE_URL` to your deployed app URL, for example `https://mba-ethics.onrender.com`, so notification emails can load the UJ logo from `/static/img/uj_logo.png`. You can override only the email logo with `MAIL_LOGO_URL`.

## Document Storage

New MBA and Ethics uploads are stored in PostgreSQL as binary data. The app keeps filesystem fallback support for legacy local files.

After upgrading an existing local database, run:

```powershell
flask --app run.py backfill-document-bytes
```

## Demo Data

The optional demo seed uses generated passwords by default:

```powershell
flask --app run.py seed-demo
```

Set `MBA_DEMO_SEED_PASSWORD` locally before running the command if you need a stable demo password. Do not commit demo passwords.

## Pre-Push Checklist

- Keep `.env` and upload folders untracked.
- Run `git diff --check`.
- Run `python -m compileall app migrations`.
- Run `flask --app run.py routes`.
- Run a secret scan before pushing and rotate any value that was ever committed locally.
