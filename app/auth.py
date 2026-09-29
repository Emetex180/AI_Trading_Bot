"""Authentication and role-based access control.

Session-based, using Flask's own signed cookie plus Werkzeug's password
hashing — both already present as Flask dependencies, so the client-facing
platform adds no new auth library.

The three properties this module exists to guarantee:

* **Passwords are never stored recoverably.** Only
  :func:`werkzeug.security.generate_password_hash` output reaches the database.
  There is no code path anywhere that writes a plaintext password to a column.
* **A client cannot reach the admin area.** :func:`admin_required` refuses a
  logged-in client on both HTML routes (redirect) and API routes (401/403 JSON),
  so it cannot be bypassed by calling the endpoint directly.
* **Failures do not leak whether an account exists.** An unknown username costs
  the same work as a wrong password and returns the same message, and repeated
  failures are throttled per username+address.

Registration order matters: :func:`register_auth` must be called *after* the
``before_request`` hook that opens ``g.repo`` (see ``app.web.create_app``),
because the session loader reads the user row through that repository.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import timedelta
from functools import wraps
from urllib.parse import urlsplit

from flask import (Blueprint, current_app, flash, g, jsonify, redirect,
                   render_template, request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from database import models as m
from trading import time_utils as tu

log = logging.getLogger(__name__)

#: Session key holding the logged-in user's primary key.
_SESSION_USER_ID = "uid"

#: Session key holding the password stamp the session was created against.
#: Compared on every request by :func:`_load_user_from_session`; a mismatch
#: means the password changed after this session began, so the session is
#: dropped. See ``User.password_changed_at``.
_SESSION_PWD_KEY = "pwd"

#: A pre-computed hash of a random value, verified against when the username is
#: unknown. Without it, a missing user returns visibly faster than a wrong
#: password and the response time alone enumerates valid usernames.
_DUMMY_HASH = generate_password_hash(secrets.token_urlsafe(32))

#: Minimum length for a *newly set* password. Read from settings where available;
#: this is the floor used by the CLI and tests that have no settings object.
FALLBACK_MIN_PASSWORD = 12

auth_bp = Blueprint("auth", __name__)


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    """Hash a password for storage (never returns the input)."""
    return generate_password_hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """Check a password against a stored hash, in constant time.

    ``hmac.compare_digest``-grade comparison comes from Werkzeug itself; a
    missing hash still performs a dummy verify so the timing does not differ.
    """
    if not password_hash:
        check_password_hash(_DUMMY_HASH, password)
        return False
    return check_password_hash(password_hash, password)


def password_problem(password: str, *, minimum: int | None = None) -> str | None:
    """Why this password is unacceptable, or ``None`` if it is fine.

    Length only. Composition rules ("one symbol, one digit") are deliberately
    not imposed: they push people towards predictable substitutions without
    adding real strength, and length is the property that actually matters.
    """
    limit = minimum if minimum is not None else FALLBACK_MIN_PASSWORD
    if not password or not password.strip():
        return "Password cannot be empty."
    if len(password) < limit:
        return f"Password must be at least {limit} characters."
    if password.strip() != password:
        return "Password cannot start or end with a space."
    return None


# --------------------------------------------------------------------------- #
# Login throttle
# --------------------------------------------------------------------------- #
class LoginThrottle:
    """Per-key failed-login limiter.

    In-process and therefore per-worker. That is sufficient here because the
    production server (``run.py serve``) runs a single Waitress process, and its
    purpose is to blunt online guessing rather than to be an exact accounting
    system. A restart clears it, which is an acceptable trade for not adding a
    shared store to a SQLite-backed app.
    """

    def __init__(self, max_attempts: int = 5, lockout_minutes: int = 15,
                 now=time.monotonic):
        self.max_attempts = max(1, max_attempts)
        self.window = max(1, lockout_minutes) * 60
        self._now = now
        self._fails: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str) -> deque[float]:
        cutoff = self._now() - self.window
        bucket = self._fails[key]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        return bucket

    def retry_after(self, key: str) -> int:
        """Seconds until this key may try again; ``0`` when it may try now."""
        with self._lock:
            bucket = self._prune(key)
            if len(bucket) < self.max_attempts:
                return 0
            return max(1, int(bucket[0] + self.window - self._now()) + 1)

    def record_failure(self, key: str) -> None:
        with self._lock:
            self._prune(key).append(self._now())

    def reset(self, key: str) -> None:
        """Clear the record for a key after a successful login."""
        with self._lock:
            self._fails.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._fails.clear()


def throttle_key(username: str) -> str:
    """Throttle bucket for one login attempt: the username *and* the caller.

    Both, so one attacker cannot lock a known account out from elsewhere, and
    one address cannot cycle usernames to sidestep the limit.
    """
    address = (request.remote_addr or "?") if request else "?"
    return f"{(username or '').strip().lower()}|{address}"


# --------------------------------------------------------------------------- #
# CSRF
# --------------------------------------------------------------------------- #
#: Form field and header a state-changing request must carry.
CSRF_FIELD = "_csrf"
CSRF_HEADER = "X-CSRF-Token"

#: Session key holding this session's token.
_CSRF_KEY = "csrf"


def csrf_token() -> str:
    """This session's CSRF token, generated on first use.

    Bound to the *session* rather than to the user, so it exists on the login
    form too — login CSRF (forcing a victim into the attacker's account) is a
    real attack and is worth blocking on the one form that has no session user
    yet.

    Rotated on login: :func:`login_user` clears the session, and the next call
    here mints a fresh token, so a token captured before authentication cannot
    be replayed after it.
    """
    token = session.get(_CSRF_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[_CSRF_KEY] = token
    return token


def csrf_ok() -> bool:
    """Is the current request's CSRF token valid?

    Accepts the token from a form field or the ``X-CSRF-Token`` header, so both
    a normal POST and a ``fetch()`` from the app's own JavaScript can satisfy
    it. Compared in constant time.
    """
    expected = session.get(_CSRF_KEY)
    if not expected:
        # No token was ever issued to this session, so there is nothing a
        # request could present that would match. Refuse rather than accept.
        return False
    supplied = (request.form.get(CSRF_FIELD)
                or request.headers.get(CSRF_HEADER) or "")
    return bool(supplied) and hmac.compare_digest(str(supplied), str(expected))


def require_csrf():
    """Refuse the request unless its CSRF token is valid.

    Returns a response to return, or ``None`` when the request may proceed.
    Shaped that way so a view reads as::

        if (denied := require_csrf()) is not None:
            return denied
    """
    if csrf_ok():
        return None
    log.warning("CSRF token missing or invalid for %s %s from %s",
                request.method, request.path, request.remote_addr)
    return jsonify({"ok": False, "reason": "csrf_failed",
                    "message": "Your session token has expired. Reload the "
                               "page and try again."}), 400


# --------------------------------------------------------------------------- #
# Current user / session
# --------------------------------------------------------------------------- #
def current_user() -> m.User | None:
    """The logged-in user for this request, or ``None``."""
    return getattr(g, "user", None)


def password_stamp(user: m.User) -> str:
    """The password marker a session records, as a string.

    Empty for an account that has never had its password changed through a path
    that stamps the column — which includes every account that predates it. An
    absent session value also reads as ``""``, so those sessions keep working.
    """
    when = getattr(user, "password_changed_at", None)
    return when.isoformat() if when else ""


def login_user(user: m.User) -> None:
    """Establish a session for ``user``.

    The session stores only the primary key plus the password stamp. Everything
    else — role, status, display name — is re-read from the database on every
    request, so suspending an account takes effect immediately instead of at
    session expiry.
    """
    session.clear()
    session[_SESSION_USER_ID] = user.id
    session[_SESSION_PWD_KEY] = password_stamp(user)
    session.permanent = True


def logout_user() -> None:
    session.clear()


def change_password(repo, user: m.User, new_password: str) -> bool:
    """Change ``user``'s password and keep *this* session signed in.

    The repository stamps ``password_changed_at``, which invalidates every
    session that recorded an older stamp — including, on its face, the one making
    this request. That is the right outcome for every other session and the wrong
    one here: the person who just chose a new password should not be bounced to
    the login page by their own change, and the flash confirming it would be lost
    if they were.

    Re-issuing the session afterwards gives this browser the new stamp while
    every other session keeps the old one and dies on its next request. The
    rotation that :func:`login_user` performs on the way through also mints a
    fresh CSRF token, which is wanted: the token issued before an authentication
    event should not outlive it.
    """
    if not repo.set_user_password(user.id, new_password):
        return False
    fresh = repo.get_user(user.id)
    if fresh is None:  # pragma: no cover - the update above just succeeded
        return False
    login_user(fresh)
    return True


def _load_user_from_session() -> m.User | None:
    user_id = session.get(_SESSION_USER_ID)
    if not user_id:
        return None
    repo = getattr(g, "repo", None)
    if repo is None:  # pragma: no cover - create_app always registers one first
        return None
    user = repo.get_user(user_id)
    if user is None or not user.is_active:
        # Deleted or suspended mid-session: drop the cookie rather than leaving
        # a half-valid session that fails later on a different route.
        session.clear()
        return None
    # The password changed after this session was issued, so this cookie is a
    # credential the account holder has already replaced. Dropping it here is
    # what makes "reset your password" actually end an attacker's session.
    if (session.get(_SESSION_PWD_KEY) or "") != password_stamp(user):
        session.clear()
        return None
    return user


# --------------------------------------------------------------------------- #
# Guards
# --------------------------------------------------------------------------- #
def _wants_json() -> bool:
    """Is the caller a JSON client rather than a browser navigation?

    Guards answer a fetch() with a status code and a browser with a redirect;
    sending a login page to the first (or a 302 to the second) would both be
    wrong. ``Accept`` decides it, with the ``/api/`` prefix as a backstop.
    """
    if request.path.startswith("/api/"):
        return True
    accept = request.headers.get("Accept", "")
    return "application/json" in accept and "text/html" not in accept


def _refuse(message: str, code: int, *, redirect_to_login: bool):
    if redirect_to_login and not _wants_json():
        return redirect(url_for("auth.login", next=request.full_path
                                if request.query_string else request.path))
    return jsonify({"ok": False, "reason": message, "message": message}), code


def login_required(view):
    """Any authenticated user."""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if current_user() is None:
            return _refuse("authentication_required", 401, redirect_to_login=True)
        return view(*args, **kwargs)
    return wrapper


def admin_required(view):
    """Platform admins only.

    A logged-in *client* gets 403, not a redirect: they are authenticated and
    the answer to "may I see this" is a flat no, which is both truthful and
    keeps a client from being bounced through a login page they already passed.
    """
    @wraps(view)
    def wrapper(*args, **kwargs):
        user = current_user()
        if user is None:
            return _refuse("authentication_required", 401, redirect_to_login=True)
        if not user.is_admin:
            return _refuse("admin_required", 403, redirect_to_login=False)
        return view(*args, **kwargs)
    return wrapper


def client_required(view):
    """A logged-in client. Admins are allowed through as well.

    An admin is not locked out of the client dashboard — they need to be able to
    see what clients see — but a client is never allowed into the admin area.
    """
    @wraps(view)
    def wrapper(*args, **kwargs):
        if current_user() is None:
            return _refuse("authentication_required", 401, redirect_to_login=True)
        return view(*args, **kwargs)
    return wrapper


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
def _safe_next(target: str | None) -> str | None:
    """A same-site path from the ``next`` parameter, or ``None``.

    Rejects absolute URLs and protocol-relative ``//host`` forms, so the login
    page cannot be turned into an open redirect by a crafted link.
    """
    if not target:
        return None
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return None
    if not target.startswith("/") or target.startswith("//"):
        return None
    return target


def _landing_for(user: m.User) -> str:
    """Where a user belongs after logging in.

    An admin goes to the admin area. A client goes to the trading dashboard when
    their subscription actually reaches it, and otherwise to the subscription
    page — landing a brand-new account on a page it is not entitled to would
    answer every sign-up with a 403 upgrade prompt, which reads as a fault
    rather than as "choose a plan".

    The access layer is imported here rather than at module scope because
    :mod:`app.access` imports this module; a top-level import would be circular.
    """
    if user.is_admin:
        return url_for("admin.index")
    from .access import FEATURE_DASHBOARD, has_feature
    if has_feature(user, FEATURE_DASHBOARD):
        return url_for("client.overview")
    return url_for("subscription.index")


@auth_bp.get("/login")
def login():
    if current_user() is not None:
        return redirect(_landing_for(current_user()))
    return render_template("login.html", next=_safe_next(request.args.get("next")))


@auth_bp.post("/login")
def login_post():
    repo = g.repo
    cfg = current_app.config["CFG"]
    throttle: LoginThrottle = current_app.extensions["login_throttle"]

    # The sign-in field accepts either a username or an email address, so the
    # same form serves an account an admin issued (which has a username) and one
    # that signed itself up (which was created from an email). ``username`` is
    # still read as the field name so existing forms and bookmarks keep working.
    username = (request.form.get("identifier")
                or request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    target = _safe_next(request.form.get("next"))

    def fail(message: str, code: int = 401):
        return render_template("login.html", error=message, username=username,
                               next=target), code

    # Checked before the throttle so a forged cross-site POST cannot consume a
    # real user's attempt budget and lock them out.
    if not csrf_ok():
        repo.log_event("WARN", "auth",
                       f"login rejected: bad CSRF token from "
                       f"{request.remote_addr}")
        return fail("Your session expired. Reload the page and try again.", 400)

    key = throttle_key(username)
    wait = throttle.retry_after(key)
    if wait:
        repo.log_event("WARN", "auth",
                       f"login throttled for {username!r} from "
                       f"{request.remote_addr}")
        return fail(f"Too many failed attempts. Try again in {wait} second"
                    f"{'s' if wait != 1 else ''}.", 429)

    # Username first, then email. Both are case-insensitive, and neither lookup
    # reveals which one matched — the message below is identical either way.
    user = (repo.get_user_by_username(username)
            or repo.get_user_by_email(username))
    # The same message and the same hashing cost whether or not the account
    # exists, so a response cannot be used to enumerate usernames.
    if user is None or not verify_password(password, user.password_hash):
        throttle.record_failure(key)
        repo.log_event("WARN", "auth",
                       f"failed login for {username!r} from "
                       f"{request.remote_addr}")
        return fail("Invalid email or password.")

    if not user.is_active:
        repo.log_event("WARN", "auth", f"login refused for suspended {username!r}")
        return fail("This account is suspended. Contact your administrator.", 403)

    throttle.reset(key)
    login_user(user)
    repo.touch_user_login(user.id)
    repo.log_event("INFO", "auth",
                   f"{user.username} ({user.role}) signed in from "
                   f"{request.remote_addr}")
    return redirect(target or _landing_for(user))


@auth_bp.post("/logout")
def logout():
    # CSRF-checked like any other state change: without it, a third-party page
    # could sign a user out at will, which is disruptive rather than dangerous
    # but still not something another origin should be able to do.
    if not csrf_ok():
        return redirect(url_for("auth.login"))
    user = current_user()
    if user is not None:
        g.repo.log_event("INFO", "auth", f"{user.username} signed out")
    logout_user()
    return redirect(url_for("auth.login"))


# --------------------------------------------------------------------------- #
# Self-registration
# --------------------------------------------------------------------------- #
#: Deliberately permissive: a single ``@`` with something either side and a dot
#: in the domain. The authoritative test of an address is whether mail to it
#: arrives, and a stricter regex mostly rejects real addresses (plus-addressing,
#: new TLDs, apostrophes) without stopping anyone who wants to enter a fake one.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")

#: Longest address the ``email`` column holds (String(254)).
MAX_EMAIL_LENGTH = 254


def email_problem(email: str) -> str | None:
    """Why this email is unusable, or ``None`` if it looks fine."""
    value = (email or "").strip()
    if not value:
        return "Email is required."
    if len(value) > MAX_EMAIL_LENGTH:
        return "That email address is too long."
    if not _EMAIL_RE.match(value):
        return "Enter a valid email address."
    return None


def username_from_email(repo, email: str) -> str:
    """A unique username derived from an email address.

    Registration asks for an email, but ``users.username`` is NOT NULL and
    UNIQUE and predates self-signup, so one has to be produced. The local part
    is used where it is usable, and a numeric suffix appended until it is free —
    ``ada``, ``ada2``, ``ada3``. The user never sees it unless they look, since
    sign-in accepts the email address.
    """
    local = (email or "").strip().split("@")[0].lower()
    # Keep it to characters that are safe in a URL and readable in the admin
    # table; anything else collapses to a dash.
    base = re.sub(r"[^a-z0-9._-]+", "-", local).strip("-._")[:48] or "trader"
    candidate, suffix = base, 1
    while repo.login_identifier_exists(candidate):
        suffix += 1
        candidate = f"{base}{suffix}"[:64]
    return candidate


def _notify_registration(repo, user: m.User) -> None:
    """Record the sign-up in-app and send the welcome email.

    Every step is best-effort. A registration that succeeded must not be
    reported as failed because a mail provider was unreachable, and the account
    is already committed by the time this runs — so a failure here is logged and
    swallowed rather than raised.
    """
    try:
        repo.notify(user.id, kind=m.NOTIFY_REGISTRATION, title="Welcome to 3rader",
                    body="Your account is ready. Choose a plan to unlock the "
                         "platform.",
                    link="/subscription")
    except Exception:  # pragma: no cover - notification is never load-bearing
        log.exception("Could not record the registration notification for %s",
                      user.id)

    try:
        from notifications.email import send_welcome
        send_welcome(user)
    except Exception:  # pragma: no cover - see the docstring
        log.exception("Could not send the welcome email to %s",
                      getattr(user, "email", "?"))


def _registration_throttle():
    """Per-address limiter for account creation and reset requests.

    Separate from the login throttle so a burst of sign-ups cannot consume a
    real user's failed-login budget, and vice versa. Its purpose is to blunt
    automated abuse of a route that sends mail and inserts rows.
    """
    from flask import current_app
    return current_app.extensions["register_throttle"]


@auth_bp.get("/register")
def register():
    if current_user() is not None:
        return redirect(_landing_for(current_user()))
    return render_template("register.html")


@auth_bp.post("/register")
def register_post():
    repo = g.repo
    cfg = current_app.config["CFG"]
    throttle: LoginThrottle = _registration_throttle()

    form = {
        "full_name": (request.form.get("full_name") or "").strip(),
        "email": (request.form.get("email") or "").strip(),
        "terms": request.form.get("terms"),
    }

    def fail(message: str, code: int = 400):
        return render_template("register.html", error=message, form=form), code

    if not csrf_ok():
        return fail("Your session expired. Reload the page and try again.", 400)

    key = f"register|{request.remote_addr or '?'}"
    wait = throttle.retry_after(key)
    if wait:
        return fail(f"Too many attempts. Try again in {wait} second"
                    f"{'s' if wait != 1 else ''}.", 429)
    throttle.record_failure(key)   # counted on attempt, not on failure

    if not form["full_name"]:
        return fail("Enter your full name.")
    if not form["terms"]:
        return fail("Please accept the terms and the risk disclaimer.")

    problem = email_problem(form["email"])
    if problem:
        return fail(problem)

    password = request.form.get("password") or ""
    confirm = request.form.get("confirm_password") or ""
    problem = password_problem(password, minimum=cfg.min_password_length)
    if problem:
        return fail(problem)
    if password != confirm:
        return fail("Those passwords do not match.")

    if repo.login_identifier_exists(form["email"]):
        # This one *does* confirm the address is taken, which is a deliberate
        # trade: without it, sign-up either fails silently or creates a
        # duplicate. Recovery is the place where enumeration is worth avoiding,
        # because there the attacker learns something they can act on.
        return fail("An account with that email already exists. Try signing in.")

    user = repo.create_user(
        username=username_from_email(repo, form["email"]),
        password_hash_or_plain=password,
        role=m.ROLE_CLIENT,
        email=form["email"],
        display_name=form["full_name"],
        created_by="self-registration",
        subscribe=True,
    )
    repo.log_event("INFO", "auth",
                   f"new registration {user.username!r} from "
                   f"{request.remote_addr}")

    throttle.reset(key)
    login_user(user)
    repo.touch_user_login(user.id)
    _notify_registration(repo, user)
    flash("Welcome to 3rader. Choose a plan to get started.", "success")
    return redirect(url_for("subscription.index"))


# --------------------------------------------------------------------------- #
# Password recovery
# --------------------------------------------------------------------------- #
#: How long a reset link stays usable. Short enough that a link sitting in an
#: inbox is not a standing credential, long enough to survive a spam-folder
#: delay.
RESET_TTL_MINUTES = 30


def _send_reset_email(user: m.User, link: str) -> bool:
    """Hand the reset link to the mail layer. Never raises."""
    try:
        from notifications.email import send_password_reset
        return bool(send_password_reset(user, link))
    except Exception:  # pragma: no cover - see the docstring
        log.exception("Could not send the password reset email to %s",
                      getattr(user, "email", "?"))
        return False


def _reset_url(token: str) -> str:
    """Absolute URL for a reset link, built from APP_URL where configured.

    ``_external=True`` alone would build the link from the Host header, which is
    attacker-controlled on a misconfigured deployment — enough to mail a victim
    a link pointing at someone else's server. APP_URL is the configured origin
    and is preferred whenever it is set.
    """
    cfg = current_app.config["CFG"]
    base = (getattr(cfg, "app_url", "") or "").strip().rstrip("/")
    path = url_for("auth.reset_password", token=token)
    return f"{base}{path}" if base else url_for("auth.reset_password",
                                                token=token, _external=True)


@auth_bp.get("/forgot-password")
def forgot_password():
    return render_template("forgot_password.html")


@auth_bp.post("/forgot-password")
def forgot_password_post():
    repo = g.repo
    throttle: LoginThrottle = _registration_throttle()

    def done():
        # The same page, with the same message, whether or not the address is
        # registered. Recovery must not be an account-enumeration oracle: the
        # answer here is the only thing an attacker would be reading.
        return render_template("forgot_password.html", sent=True)

    if not csrf_ok():
        return done()
    key = f"forgot|{request.remote_addr or '?'}"
    if throttle.retry_after(key):
        return done()
    throttle.record_failure(key)

    email = (request.form.get("email") or "").strip()
    user = repo.get_user_by_email(email) if email_problem(email) is None else None

    if user is not None and user.is_active:
        token = secrets.token_urlsafe(32)
        expires = tu.now_utc() + timedelta(minutes=RESET_TTL_MINUTES)
        repo.create_password_reset(
            user_id=user.id,
            # Only the digest is stored, so the database never holds a working
            # link. Same reasoning as storing password hashes and not passwords.
            token_hash=hashlib.sha256(token.encode()).hexdigest(),
            expires_at=expires)
        delivered = _send_reset_email(user, _reset_url(token))
        repo.log_event("INFO", "auth",
                       f"password reset requested for {user.username!r}"
                       f"{'' if delivered else ' (email not delivered)'}")
    else:
        repo.log_event("INFO", "auth",
                       f"password reset requested for unknown address from "
                       f"{request.remote_addr}")

    return done()


def _reset_token_problem(token: str) -> str | None:
    """Whether a reset link is unusable, without consuming it."""
    return None if token else "That reset link is not valid."


@auth_bp.get("/reset-password/<token>")
def reset_password(token: str):
    return render_template("reset_password.html", token=token)


@auth_bp.post("/reset-password/<token>")
def reset_password_post(token: str):
    repo = g.repo
    cfg = current_app.config["CFG"]

    def fail(message: str, code: int = 400):
        return render_template("reset_password.html", token=token,
                               error=message), code

    if not csrf_ok():
        return fail("Your session expired. Reload the page and try again.", 400)
    if _reset_token_problem(token):
        return fail("That reset link is not valid.")

    password = request.form.get("password") or ""
    confirm = request.form.get("confirm_password") or ""
    problem = password_problem(password, minimum=cfg.min_password_length)
    if problem:
        return fail(problem)
    if password != confirm:
        return fail("Those passwords do not match.")

    # Consumed only once the new password has passed every check, so a rejected
    # attempt does not burn the link and force another email.
    user = repo.consume_password_reset(hashlib.sha256(token.encode()).hexdigest())
    if user is None:
        return fail("That reset link has expired or has already been used. "
                    "Request a new one.", 410)

    # set_user_password stamps ``password_changed_at``, which invalidates every
    # session issued before now — including one an attacker may be holding.
    repo.set_user_password(user.id, password)
    logout_user()
    repo.log_event("INFO", "auth", f"password reset completed for {user.username!r}")
    flash("Your password has been changed. Sign in with your new password.",
          "success")
    return redirect(url_for("auth.login"))


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
def bootstrap_admin(repo, cfg) -> None:
    """Create the first admin from the environment, once.

    Only fires while the users table is completely empty, so it can never
    overwrite or re-create an account on a live system, and it never runs at all
    unless ``ADMIN_USERNAME`` and ``ADMIN_PASSWORD`` are both set.
    """
    if repo.count_users():
        return
    username = (cfg.bootstrap_admin_username or "").strip()
    password = cfg.bootstrap_admin_password or ""
    if not username or not password:
        return

    problem = password_problem(password, minimum=cfg.min_password_length)
    if problem:
        # Configuration error, and deliberately loud: silently creating a weaker
        # admin than asked for would be worse than refusing to create one.
        log.warning("ADMIN_PASSWORD rejected (%s); no admin account created. "
                    "Set a longer ADMIN_PASSWORD in .env and restart.", problem)
        return

    repo.create_user(username=username, password_hash_or_plain=password,
                     role=m.ROLE_ADMIN,
                     email=(cfg.bootstrap_admin_email or "").strip(),
                     created_by="bootstrap")
    log.info("Bootstrap admin %r created from ADMIN_USERNAME/ADMIN_PASSWORD.",
             username)


def register_auth(app) -> None:
    """Wire session config, the user loader, the throttle and the auth routes.

    Must be called after the ``before_request`` hook that populates ``g.repo``.
    """
    cfg = app.config["CFG"]

    secret = (cfg.flask_secret_key or "").strip()
    if not secret:
        # A random key keeps the cookie *signed* rather than forgeable, which is
        # the property that matters; the cost is that sessions do not survive a
        # restart. Warned rather than fatal so a local run still works, and the
        # deployment guide requires the key in production.
        secret = secrets.token_urlsafe(48)
        log.warning("FLASK_SECRET_KEY is not set — generated a temporary one. "
                    "Sessions will be invalidated on every restart. Set "
                    "FLASK_SECRET_KEY in .env before serving clients.")
    app.secret_key = secret

    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,      # no JavaScript access to the session
        SESSION_COOKIE_SAMESITE="Lax",     # blocks cross-site POSTs with the cookie
        SESSION_COOKIE_SECURE=bool(cfg.session_cookie_secure),
        PERMANENT_SESSION_LIFETIME=timedelta_hours(cfg.session_lifetime_hours),
    )

    app.extensions["login_throttle"] = LoginThrottle(
        max_attempts=cfg.login_max_attempts,
        lockout_minutes=cfg.login_lockout_minutes,
    )
    # Sign-up and password-recovery run on their own budget. A separate limiter
    # so a script hammering /register cannot lock a real user out of signing in,
    # and vice versa. Tighter than the login policy because both routes insert
    # rows or send mail, which are the two things worth abusing here.
    app.extensions["register_throttle"] = LoginThrottle(
        max_attempts=max(1, int(getattr(cfg, "register_max_attempts", 5) or 5)),
        lockout_minutes=max(1, int(getattr(cfg, "register_lockout_minutes", 60)
                                  or 60)),
    )

    @app.before_request
    def _load_user():
        g.user = _load_user_from_session()

    @app.context_processor
    def _inject_user():
        """Make the logged-in user and a CSRF token available to every template.

        Both travel together because every form that acts on the user's behalf
        needs both: the user decides *what* to render, the token makes the form
        submittable. Exposing the token here means no template has to remember
        to ask for it.
        """
        return {"current_user": current_user(), "csrf_token": csrf_token,
                # Surfaced so a recovery page can state the real expiry rather
                # than a number typed into the template that would silently drift
                # away from RESET_TTL_MINUTES the first time it is changed.
                "reset_ttl_minutes": RESET_TTL_MINUTES}

    app.register_blueprint(auth_bp)


def timedelta_hours(hours: int):
    """``timedelta`` for a config value, floored at one hour."""
    from datetime import timedelta

    return timedelta(hours=max(1, int(hours or 1)))
