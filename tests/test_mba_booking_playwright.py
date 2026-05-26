import threading
from datetime import datetime

import pytest
from sqlalchemy.pool import StaticPool
from werkzeug.serving import make_server

playwright_sync = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_sync.sync_playwright
expect = playwright_sync.expect

from app.app_factory import create_app
from app.extensions import db
from app.models import (
    MbaProject,
    MbaRole,
    MbaScholarProfile,
    MbaScholarRole,
    MbaStudentProfile,
    MbaUser,
)


class BookingPlaywrightConfig:
    TESTING = True
    SECRET_KEY = "booking-playwright-test"
    WTF_CSRF_ENABLED = False
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {
        "connect_args": {"check_same_thread": False},
        "poolclass": StaticPool,
    }
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SERVER_NAME = None
    MICROSOFT_CLIENT_ID = None
    MICROSOFT_CLIENT_SECRET = None
    MICROSOFT_TENANT_ID = "common"
    MICROSOFT_REDIRECT_URI = None
    MAIL_SERVER = None
    MAIL_PORT = 587
    MAIL_USE_TLS = True
    MAIL_USE_SSL = False
    MAIL_USERNAME = None
    MAIL_PASSWORD = None
    MAIL_DEFAULT_SENDER = None
    MAIL_LOGO_URL = None
    MAIL_TIMEOUT = 1


class ServerThread(threading.Thread):
    def __init__(self, app):
        super().__init__(daemon=True)
        self.server = make_server("127.0.0.1", 0, app)
        self.port = self.server.server_port
        self.app = app

    def run(self):
        with self.app.app_context():
            self.server.serve_forever()

    def stop(self):
        self.server.shutdown()


@pytest.fixture
def seeded_app():
    app = create_app(BookingPlaywrightConfig)
    with app.app_context():
        db.create_all()

        admin = MbaUser(
            email="booking.admin@example.test",
            role=MbaRole.MAIN_ADMIN.value,
            first_name="Booking",
            last_name="Admin",
            has_profile=True,
            popia_confirmed_at=datetime.utcnow(),
        )
        admin.set_password("Password123!")

        supervisor = MbaUser(
            email="supervisor@example.test",
            role=MbaRole.SCHOLAR.value,
            scholar_role=MbaScholarRole.SUPERVISOR.value,
            first_name="Sarah",
            last_name="Supervisor",
            has_profile=True,
            popia_confirmed_at=datetime.utcnow(),
        )
        supervisor.set_password("Password123!")

        student = MbaUser(
            email="student@example.test",
            role=MbaRole.STUDENT.value,
            first_name="Sam",
            last_name="Student",
            has_profile=True,
            popia_confirmed_at=datetime.utcnow(),
        )
        student.set_password("Password123!")

        db.session.add_all([admin, supervisor, student])
        db.session.flush()

        db.session.add_all(
            [
                MbaScholarProfile(user_id=supervisor.id, name="Sarah", surname="Supervisor"),
                MbaStudentProfile(user_id=student.id, name="Sam", surname="Student", student_number="12345678"),
                MbaProject(
                    student_id=student.id,
                    primary_supervisor_id=supervisor.id,
                    project_title="Booking Conflict Test Project",
                    project_description="Project used by Playwright to verify panel booking rules.",
                    discipline="Operations",
                    qualification="MBA",
                ),
            ]
        )
        db.session.commit()

    yield app


@pytest.fixture
def live_server(seeded_app):
    server = ServerThread(seeded_app)
    server.start()
    yield f"http://127.0.0.1:{server.port}"
    server.stop()


def login(page, base_url, email):
    page.goto(f"{base_url}/login?system=mba")
    page.locator('input[name="email"]').fill(email)
    page.locator('input[name="password"]').fill("Password123!")
    page.get_by_role("button", name="Sign in").click()


def test_mba_booking_release_autofill_and_supervisor_student_conflict(live_server):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()

        login(page, live_server, "booking.admin@example.test")
        page.goto(f"{live_server}/mba/booking")
        expect(page.get_by_role("heading", name="Create Booking Date")).to_be_visible()
        page.locator("#adminDate").fill("2026-06-01")
        page.locator("#adminPanels").fill("Panel 1, Panel 2")
        page.locator("#adminStudentSlots").fill("09:00")
        page.locator("#adminSupervisorSlots").fill("Slot A")
        page.get_by_role("button", name="Save Date").click()
        page.get_by_role("button", name="Release Page").click()
        expect(page.locator("#releaseState")).to_contain_text("released")
        page.get_by_text("Sign out").click()

        login(page, live_server, "student@example.test")
        page.goto(f"{live_server}/mba/booking")
        expect(page.locator("#firstName")).to_have_value("Sam")
        expect(page.locator("#surname")).to_have_value("Student")
        expect(page.locator("#role")).to_have_value("student")
        expect(page.locator("#supervisorName")).to_have_value("Sarah Supervisor")
        page.locator("#date").select_option("2026-06-01")
        page.locator("#panel").select_option("Panel 1")
        page.locator("#slot").select_option("09:00")
        page.get_by_role("button", name="Confirm Booking").click()
        expect(page.locator("#message")).to_contain_text("Booking confirmed")
        expect(page.locator("#schedule").get_by_text("Sam Student")).to_be_visible()
        page.get_by_text("Sign out").click()

        login(page, live_server, "supervisor@example.test")
        page.goto(f"{live_server}/mba/booking")
        expect(page.locator("#firstName")).to_have_value("Sarah")
        expect(page.locator("#surname")).to_have_value("Supervisor")
        expect(page.locator("#role")).to_have_value("supervisor")
        page.locator("#date").select_option("2026-06-01")
        page.locator("#panel").select_option("Panel 1")
        expect(page.locator("#slot option").first).to_contain_text("Conflict")

        page.locator("#panel").select_option("Panel 2")
        expect(page.locator("#slot option").first).to_have_text("Slot A")
        page.locator("#slot").select_option("Slot A")
        page.get_by_role("button", name="Confirm Booking").click()
        expect(page.locator("#message")).to_contain_text("Booking confirmed")
        expect(page.locator("#schedule").get_by_text("Sarah Supervisor")).to_be_visible()

        browser.close()
