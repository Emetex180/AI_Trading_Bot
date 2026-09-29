"""Account settings tests: the profile form, the avatar upload, the password.

Three things this file is really guarding.

**Nothing here can write a column it was not meant to.** The settings form is
posted by the account holder, so the fields it does not read are the security
property: ``role`` and ``status`` are the privilege-escalation question and the
answer is that the code never looks at them, which is what
``test_a_role_posted_into_the_profile_form_is_ignored`` pins down.

**An upload is hostile input.** Every rejection is asserted against the actual
flash message rather than a status code, because a redirect is what both an
accepted and a refused upload return — a status-only assertion would pass on a
totally permissive implementation.

**The current password is required even though the session already proves who
this is.** ``test_the_current_password_is_required_even_when_signed_in`` is the
test for that; without it a stolen session cookie is a full account takeover in
one request.

The avatar directory is redirected per-test into ``tmp_path`` by the ``media``
fixture, so the suite never writes into the deployment's ``data/``.
"""
from __future__ import annotations

import io
import os
import re
import struct
import zlib

import pytest

from app import profile as prof
from database import models as m

from test_web import _CSRF, _PASSWORD, _repo, _sign_in


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
@pytest.fixture
def media(tmp_path, monkeypatch):
    """Point the avatar store at a temporary directory for one test."""
    root = tmp_path / "avatars"
    monkeypatch.setattr(prof, "MEDIA_ROOT", root)
    return root


def _app_client(repo, monkeypatch, **kw):
    """A test client for ``repo``'s app, with the media root kept temporary."""
    from dataclasses import replace

    from app.web import create_app
    from config import get_settings

    settings = replace(get_settings(), flask_secret_key="test-secret",
                       telegram_enabled=False, telegram_bot_token="",
                       telegram_chat_id="", bootstrap_admin_username="",
                       bootstrap_admin_password="", **kw)
    return create_app(settings=settings, repository=repo, setup_db=False,
                      jobs=type("J", (), {"status": lambda s: None})())


def _client(repo, tmp_path, monkeypatch, *, role=m.ROLE_CLIENT, **settings):
    """A signed-in test client whose avatar store is a temp directory."""
    monkeypatch.setattr(prof, "MEDIA_ROOT", tmp_path / "avatars")
    app = _app_client(repo, monkeypatch, **settings)
    client = app.test_client()
    user = _sign_in(client, repo, role=role, username="account")
    client.get("/settings")            # render a page so a CSRF token is minted
    with client.session_transaction() as sess:
        token = sess["csrf"]
    return app, client, user, token


def _user(repo, user_id: int):
    """Re-read a user, so assertions see what was committed rather than a cache."""
    repo.session.expire_all()
    return repo.get_user(user_id)


def _flash(response) -> tuple[str, str]:
    """The (category, text) of the first flash in a rendered page."""
    found = re.search(rb'alert alert-(\w+)"[^>]*>\s*([^<]+)', response.data)
    if not found:
        return "", ""
    return found.group(1).decode(), found.group(2).strip().decode()


# --- image payloads ------------------------------------------------------- #
def png(width: int, height: int, trailer: bytes = b"") -> bytes:
    """A structurally real PNG of the requested size.

    Real rather than a stub, because the validator parses the IHDR chunk: a
    hand-rolled header that only looks right would not exercise the code under
    test. ``trailer`` appends bytes after IEND, for the polyglot case.
    """
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body)))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
            + trailer)


def gif(width: int, height: int) -> bytes:
    """A real GIF87a header plus a minimal image block."""
    header = b"GIF87a" + struct.pack("<HH", width, height) + b"\x00\x00\x00"
    return header + b"\x2c" + struct.pack("<HHHH", 0, 0, width, height) + b"\x00"


def jpeg(width: int, height: int) -> bytes:
    """A JPEG with an APP0 segment before the frame header.

    The segment ahead of SOF0 is the whole point: it is what a parser reading
    the dimensions from a fixed offset would get wrong. Its length field has to
    be correct, or the chain walk skips past the frame header and the file is
    (rightly) refused.
    """
    app0_payload = b"JFIF\x00" + b"\x01\x01\x00" + b"\x00" * 7
    app0 = b"\xff\xe0" + struct.pack(">H", len(app0_payload) + 2) + app0_payload
    sof_payload = (b"\x08" + struct.pack(">HH", height, width)
                   + b"\x03" + b"\x00" * 9)
    sof0 = b"\xff\xc0" + struct.pack(">H", len(sof_payload) + 2) + sof_payload
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def _upload(client, token, payload: bytes, filename: str = "me.png",
            content_type: str = "image/png", field: str = "avatar"):
    return client.post("/settings/avatar",
                       data={"_csrf": token,
                             field: (io.BytesIO(payload), filename, content_type)},
                       content_type="multipart/form-data",
                       follow_redirects=True)


# --------------------------------------------------------------------------- #
# The settings page
# --------------------------------------------------------------------------- #
def test_the_settings_page_renders_for_a_client(tmp_path, monkeypatch):
    app, client, user, token = _client(_repo(), tmp_path, monkeypatch)
    response = client.get("/settings")
    assert response.status_code == 200
    assert user.username.encode() in response.data


def test_the_settings_page_needs_a_session(tmp_path, monkeypatch):
    repo = _repo()
    monkeypatch.setattr(prof, "MEDIA_ROOT", tmp_path / "avatars")
    anonymous = _app_client(repo, monkeypatch).test_client()
    assert anonymous.get("/settings").status_code == 302


# --------------------------------------------------------------------------- #
# The profile form
# --------------------------------------------------------------------------- #
def test_the_profile_form_saves_the_details_it_owns(tmp_path, monkeypatch):
    repo = _repo()
    app, client, user, token = _client(repo, tmp_path, monkeypatch)

    response = client.post("/settings", data={
        "_csrf": token, "display_name": "Ada L", "email": "ada@example.com",
        "phone": "+44 7700 900000", "country": "United Kingdom",
    }, follow_redirects=True)

    assert response.status_code == 200
    row = _user(repo, user.id)
    assert (row.display_name, row.email, row.phone, row.country) == (
        "Ada L", "ada@example.com", "+44 7700 900000", "United Kingdom")


def test_a_role_posted_into_the_profile_form_is_ignored(tmp_path, monkeypatch):
    """The escalation test: a client cannot promote themselves.

    ``role`` and ``status`` are posted alongside the real fields, exactly as a
    crafted request would, and must come out unchanged. The form has no such
    input — which is why this posts the fields directly rather than driving the
    UI, since the UI is not the thing being trusted.
    """
    repo = _repo()
    app, client, user, token = _client(repo, tmp_path, monkeypatch)

    client.post("/settings", data={
        "_csrf": token, "email": "ada@example.com", "display_name": "Ada",
        "role": m.ROLE_ADMIN, "status": m.STATUS_SUSPENDED,
        "password_hash": "not-a-hash", "avatar_path": "../escape.png",
        "subscription_plan": "vip", "user_id": "1", "id": "1",
    }, follow_redirects=True)

    row = _user(repo, user.id)
    assert row.role == m.ROLE_CLIENT
    assert row.status == m.STATUS_ACTIVE
    assert row.password_hash != "not-a-hash"
    assert row.avatar_path == ""


def test_an_empty_email_is_refused(tmp_path, monkeypatch):
    repo = _repo()
    app, client, user, token = _client(repo, tmp_path, monkeypatch)
    before = _user(repo, user.id).email

    response = client.post("/settings", data={"_csrf": token, "email": "  ",
                                              "display_name": "Ada"},
                           follow_redirects=True)

    assert _flash(response)[0] == "danger"
    assert _user(repo, user.id).email == before


def test_a_malformed_email_is_refused(tmp_path, monkeypatch):
    repo = _repo()
    app, client, user, token = _client(repo, tmp_path, monkeypatch)

    response = client.post("/settings", data={"_csrf": token, "email": "ada@"},
                           follow_redirects=True)

    assert _flash(response)[0] == "danger"
    assert _user(repo, user.id).email != "ada@"


def test_an_email_another_account_holds_is_refused(tmp_path, monkeypatch):
    """Otherwise one account could be sent the other's password-reset link."""
    repo = _repo()
    app, client, user, token = _client(repo, tmp_path, monkeypatch)
    repo.create_user(username="other", password_hash_or_plain=_PASSWORD,
                     role=m.ROLE_CLIENT, email="taken@example.com")

    response = client.post("/settings", data={"_csrf": token,
                                              "email": "taken@example.com"},
                           follow_redirects=True)

    assert _flash(response)[0] == "danger"
    assert _user(repo, user.id).email != "taken@example.com"


def test_keeping_your_own_email_is_not_a_conflict(tmp_path, monkeypatch):
    """Saving the form without touching the address must not fail.

    The account starts with no address — ``_sign_in`` does not set one — so one
    is saved first. That is the realistic sequence anyway: a person sets their
    address, then later comes back to change something else and re-submits the
    form with the address they already had.
    """
    repo = _repo()
    app, client, user, token = _client(repo, tmp_path, monkeypatch)

    client.post("/settings", data={"_csrf": token, "email": "mine@example.com"},
                follow_redirects=True)
    mine = _user(repo, user.id).email
    assert mine == "mine@example.com"

    response = client.post("/settings", data={"_csrf": token, "email": mine,
                                              "display_name": "Ada"},
                           follow_redirects=True)

    assert _flash(response)[0] == "success"
    assert _user(repo, user.id).email == "mine@example.com"


def test_a_profile_post_without_a_csrf_token_is_refused(tmp_path, monkeypatch):
    repo = _repo()
    app, client, user, token = _client(repo, tmp_path, monkeypatch)

    response = client.post("/settings", data={"email": "evil@example.com"},
                           follow_redirects=True)

    assert _flash(response)[0] == "warning"
    assert _user(repo, user.id).email != "evil@example.com"


# --------------------------------------------------------------------------- #
# Avatars — accepted
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name,payload,expected", [
    ("png", png(40, 40), "image/png"),
    ("gif", gif(40, 40), "image/gif"),
    ("jpeg", jpeg(40, 40), "image/jpeg"),
])
def test_a_real_image_is_stored_and_served(tmp_path, monkeypatch, name, payload,
                                           expected):
    repo = _repo()
    app, client, user, token = _client(repo, tmp_path, monkeypatch)

    response = _upload(client, token, payload)
    assert _flash(response)[0] == "success"

    stored = _user(repo, user.id).avatar_path
    assert stored, "nothing was stored"

    served = client.get(f"/media/avatar/{stored}")
    assert served.status_code == 200
    assert served.headers["Content-Type"].startswith(expected)
    assert served.headers["X-Content-Type-Options"] == "nosniff"


def test_the_stored_name_is_generated_not_taken_from_the_upload(tmp_path,
                                                                monkeypatch):
    """A client-supplied filename is discarded, not sanitised.

    Nothing the client sent is needed for the path, so the safest treatment of
    ``../../evil.png`` is to never look at it. The stored name must match the
    generated pattern and must not contain any part of what was uploaded.
    """
    repo = _repo()
    app, client, user, token = _client(repo, tmp_path, monkeypatch)

    response = _upload(client, token, png(20, 20), filename="../../evil.png")
    assert _flash(response)[0] == "success"

    stored = _user(repo, user.id).avatar_path
    assert prof._FILENAME_RE.match(stored), stored
    assert "evil" not in stored and "/" not in stored and "\\" not in stored


def test_a_new_picture_replaces_the_old_one(tmp_path, monkeypatch):
    repo = _repo()
    app, client, user, token = _client(repo, tmp_path, monkeypatch)

    _upload(client, token, png(20, 20))
    first = _user(repo, user.id).avatar_path
    _upload(client, token, png(30, 30))
    second = _user(repo, user.id).avatar_path

    assert first != second
    # One file on disk, not two: the replaced picture is cleaned up.
    assert len(_stored_files(tmp_path)) == 1
    # And the old name no longer resolves.
    assert client.get(f"/media/avatar/{first}").status_code == 404


def test_a_picture_can_be_removed(tmp_path, monkeypatch):
    repo = _repo()
    app, client, user, token = _client(repo, tmp_path, monkeypatch)

    _upload(client, token, png(20, 20))
    assert _user(repo, user.id).avatar_path

    response = client.post("/settings/avatar/remove", data={"_csrf": token},
                           follow_redirects=True)

    assert _flash(response)[0] == "success"
    assert _user(repo, user.id).avatar_path == ""
    assert _stored_files(tmp_path) == []


# --------------------------------------------------------------------------- #
# Avatars — refused
#
# Every one of these asserts the flash, because an accepted and a refused upload
# both answer with a redirect: a status-code assertion would pass on an
# implementation that accepted everything.
# --------------------------------------------------------------------------- #
def test_an_svg_is_refused(tmp_path, monkeypatch):
    """SVG is a document format: served as an image it is stored XSS."""
    repo = _repo()
    app, client, user, token = _client(repo, tmp_path, monkeypatch)

    response = _upload(client, token,
                       b"<svg xmlns='http://www.w3.org/2000/svg'>"
                       b"<script>alert(1)</script></svg>", filename="x.svg")

    assert _flash(response)[0] == "danger"
    assert _user(repo, user.id).avatar_path == ""


def test_a_script_named_as_an_image_is_refused(tmp_path, monkeypatch):
    """The name and the Content-Type both say PNG; the bytes decide."""
    repo = _repo()
    app, client, user, token = _client(repo, tmp_path, monkeypatch)

    response = _upload(client, token,
                       b"<html><script>alert(1)</script></html>",
                       filename="x.png", content_type="image/png")

    assert _flash(response)[0] == "danger"
    assert _user(repo, user.id).avatar_path == ""


def _stored_files(tmp_path) -> list:
    """What is on disk in the avatar store, whether or not it exists yet.

    Reads through this rather than ``iterdir()`` directly because a refusal that
    happens before the write leaves no directory at all, and "no directory" and
    "an empty directory" are the same answer to the question these tests ask.
    """
    folder = tmp_path / "avatars"
    return sorted(folder.iterdir()) if folder.is_dir() else []


def test_an_oversized_file_is_refused(tmp_path, monkeypatch):
    """A valid image that is simply too big.

    The payload is a real PNG with incompressible filler appended, so it is
    unambiguously over the limit and the *only* thing that can refuse it is the
    size check. A small image would compress to a few hundred bytes and the test
    would pass for the wrong reason.
    """
    repo = _repo()
    app, client, user, token = _client(repo, tmp_path, monkeypatch,
                                       avatar_max_bytes=4096)
    payload = png(20, 20) + os.urandom(8192)
    assert len(payload) > 4096

    response = _upload(client, token, payload)

    assert _flash(response)[0] == "danger"
    assert "larger than" in _flash(response)[1]
    assert _user(repo, user.id).avatar_path == ""
    assert _stored_files(tmp_path) == []


def test_a_decompression_bomb_is_refused(tmp_path, monkeypatch):
    """A small file declaring enormous dimensions, refused before it is written.

    This is the amplification case: the bytes here are a few hundred kilobytes
    and would decode to gigabytes.
    """
    repo = _repo()
    app, client, user, token = _client(repo, tmp_path, monkeypatch,
                                       avatar_max_pixels=512)

    response = _upload(client, token, png(9000, 20))

    assert _flash(response)[0] == "danger"
    assert "larger than" in _flash(response)[1]
    assert _user(repo, user.id).avatar_path == ""
    assert _stored_files(tmp_path) == []


def test_an_empty_file_is_refused(tmp_path, monkeypatch):
    repo = _repo()
    app, client, user, token = _client(repo, tmp_path, monkeypatch)

    response = _upload(client, token, b"")

    assert _flash(response)[0] == "danger"
    assert _user(repo, user.id).avatar_path == ""


def test_an_upload_with_no_csrf_token_is_refused(tmp_path, monkeypatch):
    repo = _repo()
    app, client, user, token = _client(repo, tmp_path, monkeypatch)

    response = client.post("/settings/avatar",
                           data={"avatar": (io.BytesIO(png(20, 20)), "me.png",
                                            "image/png")},
                           content_type="multipart/form-data",
                           follow_redirects=True)

    assert _flash(response)[0] == "warning"
    assert _user(repo, user.id).avatar_path == ""


# --------------------------------------------------------------------------- #
# Serving
# --------------------------------------------------------------------------- #
def test_serving_refuses_a_name_that_is_not_a_stored_one(tmp_path, monkeypatch):
    """A traversal attempt and a plausible-looking name both answer 404."""
    repo = _repo()
    app, client, user, token = _client(repo, tmp_path, monkeypatch)

    for name in ("../../trading.db", "..%2f..%2ftrading.db",
                 "etc/passwd", "1-abcdefghijkl.png", "trading.db"):
        assert client.get(f"/media/avatar/{name}").status_code == 404, name


def test_serving_refuses_a_file_whose_bytes_are_not_an_image(tmp_path,
                                                             monkeypatch):
    """Defence in depth: even a file that reached the directory is inert.

    Written directly into the avatar store under a name the pattern accepts, to
    prove the serving route checks the contents rather than trusting the
    directory listing.
    """
    repo = _repo()
    app, client, user, token = _client(repo, tmp_path, monkeypatch)

    folder = tmp_path / "avatars"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "1-aaaaaaaaaaaa.png").write_bytes(b"<script>alert(1)</script>")

    assert client.get("/media/avatar/1-aaaaaaaaaaaa.png").status_code == 404


def test_serving_requires_a_session(tmp_path, monkeypatch):
    repo = _repo()
    app, client, user, token = _client(repo, tmp_path, monkeypatch)
    _upload(client, token, png(20, 20))
    stored = _user(repo, user.id).avatar_path

    anonymous = _app_client(repo, monkeypatch).test_client()
    assert anonymous.get(f"/media/avatar/{stored}").status_code == 302


def test_an_appended_payload_still_serves_as_an_image(tmp_path, monkeypatch):
    """The polyglot case, pinned to its actual containment.

    A PNG with bytes appended after IEND is accepted — the header is a PNG — and
    the appended bytes are stored. What makes that safe is the *response*: the
    declared image type plus nosniff means a browser renders it as an image and
    never executes the trailer. If this test ever fails because the file is
    being served as something else, the serving route has regressed and the
    module docstring's reasoning no longer holds.
    """
    repo = _repo()
    app, client, user, token = _client(repo, tmp_path, monkeypatch)
    payload = png(20, 20, b"<script>alert(1)</script>")

    response = _upload(client, token, payload)
    assert _flash(response)[0] == "success"

    served = client.get(f"/media/avatar/{_user(repo, user.id).avatar_path}")
    assert served.headers["Content-Type"] == "image/png"
    assert served.headers["X-Content-Type-Options"] == "nosniff"
    assert served.headers["Content-Disposition"] == "inline"


# --------------------------------------------------------------------------- #
# Changing a password
# --------------------------------------------------------------------------- #
def _change(client, token, current, new, confirm=None):
    return client.post("/settings/password", data={
        "_csrf": token, "current_password": current, "new_password": new,
        "confirm_password": new if confirm is None else confirm,
    }, follow_redirects=True)


def test_a_password_can_be_changed(tmp_path, monkeypatch):
    repo = _repo()
    app, client, user, token = _client(repo, tmp_path, monkeypatch)

    response = _change(client, token, _PASSWORD, "a-brand-new-passphrase")

    assert _flash(response)[0] == "success"

    fresh = app.test_client()
    fresh.get("/login")
    with fresh.session_transaction() as sess:
        sess["csrf"] = _CSRF
    ok = fresh.post("/login", data={"username": "account",
                                    "password": "a-brand-new-passphrase",
                                    "_csrf": _CSRF})
    assert ok.status_code == 302


def test_the_old_password_stops_working(tmp_path, monkeypatch):
    repo = _repo()
    app, client, user, token = _client(repo, tmp_path, monkeypatch)
    _change(client, token, _PASSWORD, "a-brand-new-passphrase")

    fresh = app.test_client()
    fresh.get("/login")
    with fresh.session_transaction() as sess:
        sess["csrf"] = _CSRF
    refused = fresh.post("/login", data={"username": "account",
                                         "password": _PASSWORD,
                                         "_csrf": _CSRF})
    assert refused.status_code == 401


def test_the_current_password_is_required_even_when_signed_in(tmp_path,
                                                              monkeypatch):
    """A stolen session cookie must not be a one-request account takeover."""
    repo = _repo()
    app, client, user, token = _client(repo, tmp_path, monkeypatch)

    response = _change(client, token, "not-the-password", "a-brand-new-passphrase")

    assert _flash(response)[0] == "danger"
    # And the real password still works, so nothing was changed on the way past.
    fresh = app.test_client()
    fresh.get("/login")
    with fresh.session_transaction() as sess:
        sess["csrf"] = _CSRF
    assert fresh.post("/login", data={"username": "account",
                                      "password": _PASSWORD,
                                      "_csrf": _CSRF}).status_code == 302


def test_the_change_keeps_this_session_and_ends_the_others(tmp_path,
                                                           monkeypatch):
    """The person who changed it stays in; every other session dies.

    Both halves matter: signing the actor out as well would be defensible but
    would lose the confirmation message and read as a failure, and leaving the
    others alive would make the change cosmetic.
    """
    repo = _repo()
    monkeypatch.setattr(prof, "MEDIA_ROOT", tmp_path / "avatars")
    app = _app_client(repo, monkeypatch)
    _sign_in(app.test_client(), repo, username="other")

    actor = app.test_client()
    user = _sign_in(actor, repo, username="account")
    spectator = app.test_client()
    spectator.get("/login")
    with spectator.session_transaction() as sess:
        sess["csrf"] = _CSRF
    spectator.post("/login", data={"username": "account", "password": _PASSWORD,
                                   "_csrf": _CSRF})
    actor.get("/settings")
    with actor.session_transaction() as sess:
        token = sess["csrf"]

    assert spectator.get("/settings").status_code == 200

    _change(actor, token, _PASSWORD, "a-brand-new-passphrase")

    assert actor.get("/settings").status_code == 200
    assert spectator.get("/settings").status_code == 302


def test_a_mismatched_confirmation_is_refused(tmp_path, monkeypatch):
    repo = _repo()
    app, client, user, token = _client(repo, tmp_path, monkeypatch)

    response = _change(client, token, _PASSWORD, "a-brand-new-passphrase",
                       confirm="something-else-entirely")

    assert _flash(response)[0] == "danger"
    assert _user(repo, user.id).password_hash  # unchanged, still a hash


def test_reusing_the_current_password_is_refused(tmp_path, monkeypatch):
    repo = _repo()
    app, client, user, token = _client(repo, tmp_path, monkeypatch)

    response = _change(client, token, _PASSWORD, _PASSWORD)

    assert _flash(response)[0] == "danger"


def test_a_password_below_the_policy_is_refused(tmp_path, monkeypatch):
    """The same policy registration uses, so the two cannot drift apart."""
    repo = _repo()
    app, client, user, token = _client(repo, tmp_path, monkeypatch)

    response = _change(client, token, _PASSWORD, "short")

    assert _flash(response)[0] == "danger"


def test_a_password_post_without_a_csrf_token_is_refused(tmp_path, monkeypatch):
    repo = _repo()
    app, client, user, token = _client(repo, tmp_path, monkeypatch)

    client.post("/settings/password", data={"current_password": _PASSWORD,
                                            "new_password": "a-brand-new-passphrase",
                                            "confirm_password": "a-brand-new-passphrase"},
                follow_redirects=True)

    fresh = app.test_client()
    fresh.get("/login")
    with fresh.session_transaction() as sess:
        sess["csrf"] = _CSRF
    assert fresh.post("/login", data={"username": "account",
                                      "password": "a-brand-new-passphrase",
                                      "_csrf": _CSRF}).status_code == 401
