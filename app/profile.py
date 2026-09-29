"""A user's own account page: personal details, password and profile picture.

Everything here acts on ``current_user()`` and nothing else. There is no
``user_id`` in any route, in the URL or in a form field, so there is no request
this module can be given that edits somebody else's account — a client cannot
reach another client's settings page because such a page does not exist. The
admin area has its own, separately-guarded edit paths (see :mod:`app.admin`).

Avatar uploads
--------------
An uploaded file is the one place a user can hand the server bytes of their
choosing, so it is treated as hostile input rather than as a picture:

* **The type is decided by the bytes, not by the name.** ``.png`` on a file, or
  ``Content-Type: image/png`` on the request, are both attacker-controlled and
  neither is read. The first bytes of the file must match a known image header,
  and the extension and content type the file is later served with are derived
  from that match — so a file cannot be stored as one type and served as
  another.
* **SVG is refused outright**, along with every other type not in the allow
  list. SVG is a document format that can carry script; an "image" upload that
  the browser executes as a page is stored XSS against every account that views
  it. Refusing it is the whole mitigation, and it costs nothing here because
  raster formats cover the use.
* **Dimensions are parsed from the header and bounded.** A small file can
  declare enormous dimensions and expand into a multi-gigabyte bitmap when a
  viewer decodes it (a "decompression bomb"). Reading width and height out of
  the header and refusing outliers costs nothing and closes the amplification
  before the file is ever written.
* **The stored name is generated, never supplied.** ``{user id}-{random}.{ext}``
  where the extension comes from the detected type. A client-supplied filename
  is how ``../../`` and null bytes get into a path, so it is discarded rather
  than sanitised — there is nothing it was needed for.
* **Serving is authenticated and locked down.** The file is not in the static
  directory. It is streamed by a route that requires a signed-in session, and
  the response carries the detected content type, ``inline`` disposition and
  ``X-Content-Type-Options: nosniff``.

No image library is used. This matters for one case worth naming: a file can be
a genuine PNG with other bytes appended after the image data, and the header
check below will accept it because its *header* is a PNG. Nothing here strips
those bytes, so the file is stored whole.

That is contained by the serving route, not by the sniff, and the two belong
together: :func:`avatar` answers with the content type the header declared plus
``X-Content-Type-Options: nosniff``, so a browser renders the bytes as the image
they claim to be and never as the document that was appended. Because the file
also lives outside the static directory and is only ever reached through that
route, there is no path on which the appended bytes are served under a type a
browser would execute. Re-encoding every upload through Pillow would remove the
appended bytes at rest and is the stronger design — but it adds a compiled
dependency to a deployment that currently needs none, and the vector it closes
is already closed at the point of delivery. If Pillow is added for another
reason, re-encoding here is a worthwhile follow-up; if the serving route is ever
changed to hand these files out as downloads or from disk directly, it is not.

Files live in ``data/avatars/`` beside the database, because that is the
directory the deployment guide already backs up.
"""
from __future__ import annotations

import logging
import re
import secrets
import struct
from pathlib import Path

from flask import (Blueprint, Response, current_app, flash, g, redirect,
                   render_template, request, send_file, url_for)

from config import DATA_DIR

from .auth import (change_password, client_required, csrf_ok, current_user,
                   password_problem, verify_password)

log = logging.getLogger(__name__)

profile_bp = Blueprint("profile", __name__)

#: Where avatar files are written. Beside the database, inside the directory
#: the deployment guide already backs up and excludes from version control.
MEDIA_ROOT = DATA_DIR / "avatars"

#: A stored filename: the user's id, a random token, and an extension from the
#: list below. Anchored and permitted-character-only, so a value read back out
#: of the database and joined onto the media root cannot traverse anywhere —
#: ``../`` cannot match, and neither can an absolute path or a null byte.
_FILENAME_RE = re.compile(r"^[1-9][0-9]{0,17}-[A-Za-z0-9_-]{8,64}\.(?:png|jpg|gif|webp|bmp)$")

#: Fallback ceiling if a setting is somehow missing. The configured values are
#: what apply; these only keep a misconfigured deployment bounded.
_DEFAULT_MAX_BYTES = 2 * 1024 * 1024
_DEFAULT_MAX_PIXELS = 4096


def _cfg():
    return current_app.config["CFG"]


def avatar_dir() -> Path:
    """The avatar directory, created on first use.

    Created lazily rather than at import so that importing this module — which
    happens at app start — never writes to disk, and so a read-only deployment
    only fails when someone actually uploads something.
    """
    MEDIA_ROOT.mkdir(parents=True, exist_ok=True)
    return MEDIA_ROOT


def _csrf_guard():
    """Refuse a form post whose CSRF token is missing or stale.

    Every route in this module is posted by a browser form, so a refusal is a
    flash and a return to the settings page rather than the JSON that
    :func:`app.auth.require_csrf` answers a ``fetch()`` with. The token is bound
    to the session and rotated on sign-in, so a stale one means the page was open
    across a sign-in — reloading it fixes the form, which is what the message
    says to do.
    """
    if csrf_ok():
        return None
    log.warning("CSRF token missing or invalid for %s %s", request.method,
                request.path)
    flash("Your session expired. Reload the page and try again.", "warning")
    return redirect(url_for("profile.settings"))


# --------------------------------------------------------------------------- #
# Type detection
#
# Each sniffer returns (extension, content type, width, height) or None. The
# width and height come out of the same header the type does, which is the point:
# one parse decides both what the file is and how large it claims to be.
# --------------------------------------------------------------------------- #
def _png(blob: bytes):
    if not blob.startswith(b"\x89PNG\r\n\x1a\n") or len(blob) < 24:
        return None
    if blob[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", blob[16:24])
    return "png", "image/png", width, height


def _gif(blob: bytes):
    if not blob.startswith((b"GIF87a", b"GIF89a")) or len(blob) < 10:
        return None
    width, height = struct.unpack("<HH", blob[6:10])
    return "gif", "image/gif", width, height


def _bmp(blob: bytes):
    if not blob.startswith(b"BM") or len(blob) < 26:
        return None
    # The DIB header: a signed 32-bit height, negative when the rows are stored
    # top-down. The absolute value is the dimension in both cases.
    width, height = struct.unpack("<ii", blob[18:26])
    return "bmp", "image/bmp", abs(width), abs(height)


def _webp(blob: bytes):
    if not blob.startswith(b"RIFF") or blob[8:12] != b"WEBP" or len(blob) < 30:
        return None
    chunk = blob[12:16]

    if chunk == b"VP8X":                       # extended format, canvas size
        width = int.from_bytes(blob[24:27], "little") + 1
        height = int.from_bytes(blob[27:30], "little") + 1
    elif chunk == b"VP8 ":                     # lossy, 14-bit dimensions
        width = int.from_bytes(blob[26:28], "little") & 0x3FFF
        height = int.from_bytes(blob[28:30], "little") & 0x3FFF
    elif chunk == b"VP8L":                     # lossless, packed 14-bit pairs
        bits = int.from_bytes(blob[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
    else:
        return None
    return "webp", "image/webp", width, height


def _jpeg(blob: bytes):
    """Walk the JPEG segment chain to the frame header.

    Dimensions are not at a fixed offset in a JPEG: they sit in a start-of-frame
    segment that can be preceded by any number of metadata segments (EXIF,
    thumbnails, comments). So the chain has to be followed. Every step is
    bounded by the blob length, so a truncated or malformed file ends the walk
    and returns None rather than reading past the end.
    """
    if not blob.startswith(b"\xff\xd8\xff"):
        return None

    index = 2
    while index + 9 < len(blob):
        if blob[index] != 0xFF:
            return None
        marker = blob[index + 1]
        # Padding between segments is legal and means "not a marker yet".
        if marker == 0xFF:
            index += 1
            continue
        # Standalone markers carry no length field.
        if marker in (0x01,) or 0xD0 <= marker <= 0xD9:
            index += 2
            continue

        length = struct.unpack(">H", blob[index + 2:index + 4])[0]
        if length < 2:
            return None
        # SOF0-SOF15, excluding the four that are not frame headers.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", blob[index + 5:index + 9])
            return "jpg", "image/jpeg", width, height
        index += 2 + length

    return None


#: Checked in order. Everything absent from this table — SVG, TIFF, ICO, HTML —
#: is refused, whatever it is called or claims to be.
_SNIFFERS = (_png, _jpeg, _gif, _webp, _bmp)


def sniff_image(blob: bytes):
    """Identify ``blob`` from its own bytes.

    Returns ``(extension, content_type, width, height)``, or ``None`` when the
    bytes are not one of the accepted image types.
    """
    for sniffer in _SNIFFERS:
        try:
            found = sniffer(blob)
        except Exception:
            # A malformed header must be a refusal, not a 500.
            log.warning("Avatar header parse failed", exc_info=True)
            return None
        if found is not None:
            return found
    return None


# --------------------------------------------------------------------------- #
# Upload handling
# --------------------------------------------------------------------------- #
def _limits(cfg) -> tuple[int, int]:
    max_bytes = int(getattr(cfg, "avatar_max_bytes", 0) or _DEFAULT_MAX_BYTES)
    max_pixels = int(getattr(cfg, "avatar_max_pixels", 0) or _DEFAULT_MAX_PIXELS)
    return max(max_bytes, 1024), max(max_pixels, 32)


def _read_upload(storage) -> tuple[bytes | None, str | None]:
    """Read an uploaded file, bounded by the configured size.

    Reading is capped one byte past the limit so an oversized upload is detected
    without holding all of it in memory: the form has already been parsed by the
    time this runs, but a big file is still not copied a second time.
    """
    if storage is None or not getattr(storage, "filename", ""):
        return None, "Choose an image file first."

    max_bytes, _ = _limits(_cfg())
    blob = storage.read(max_bytes + 1)
    if not blob:
        return None, "That file is empty."
    if len(blob) > max_bytes:
        return None, (f"That file is larger than {max_bytes // (1024 * 1024)} MiB. "
                      "Please choose a smaller one.")
    return blob, None


def validate_image(blob: bytes) -> tuple[tuple | None, str | None]:
    """Check ``blob`` is an acceptable image. Returns ``(detected, error)``."""
    _, max_pixels = _limits(_cfg())
    detected = sniff_image(blob)
    if detected is None:
        return None, ("That file is not a PNG, JPEG, GIF, WebP or BMP image. "
                      "Other formats are not accepted.")

    _, _, width, height = detected
    if width < 1 or height < 1:
        return None, "That image has no usable dimensions."
    if width > max_pixels or height > max_pixels:
        # Plain "x" rather than "×": this string is flashed, and a flash travels
        # through the signed session cookie and back out through whatever
        # encoding the console or a proxy assumes. ASCII removes the question.
        return None, (f"That image is {width}x{height}px, which is larger than "
                      f"the {max_pixels}px limit.")

    # The header parse above is the real check and this is belt and braces: the
    # extension and content type used from here on come from the sniff, never
    # from the upload.
    return detected, None


def store_avatar(user, blob: bytes, extension: str) -> str:
    """Write ``blob`` as ``user``'s avatar and return the stored filename.

    The name is generated, so nothing the client sent reaches the path. The
    write is atomic — a temporary file renamed into place — so a request that
    dies mid-write cannot leave a half-written image being served.
    """
    folder = avatar_dir()
    name = f"{user.id}-{secrets.token_urlsafe(12)}.{extension}"
    target = folder / name
    temp = folder / f".{name}.part"
    try:
        temp.write_bytes(blob)
        temp.replace(target)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    return name


def discard_avatar(filename: str) -> None:
    """Delete a stored avatar, if it is one we could have written.

    The filename is re-validated against the stored-name pattern before it
    touches the filesystem, so a value that somehow reached the column another
    way still cannot delete anything outside the avatar directory.
    """
    if not filename or not _FILENAME_RE.match(filename):
        return
    try:
        (avatar_dir() / filename).unlink(missing_ok=True)
    except OSError:
        # A leftover file costs a few kilobytes; failing the request over one
        # would cost the user the change they were making.
        log.warning("Could not remove avatar %r", filename, exc_info=True)


def avatar_url(user) -> str:
    """The URL of ``user``'s avatar, or ``""`` when they have none.

    Templates use the empty string as the signal to draw initials instead, so
    there is one code path for "no picture" rather than a broken image.
    """
    name = getattr(user, "avatar_path", "") or ""
    if not name:
        return ""
    return url_for("profile.avatar", filename=name)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@profile_bp.get("/settings")
@client_required
def settings():
    user = current_user()
    return render_template("client/settings.html", nav="settings", user=user,
                           avatar_max_mb=_limits(_cfg())[0] // (1024 * 1024),
                           entitlement=_entitlement(user))


def _entitlement(user):
    """The user's entitlement, for the plan card on the settings page.

    Imported here rather than at module scope to keep :mod:`app.access` free to
    import this module in turn without a cycle.
    """
    from .access import entitlement_for
    return entitlement_for(user, getattr(g, "repo", None))


@profile_bp.post("/settings")
@client_required
def settings_post():
    """Save the personal details form.

    Only the four fields in ``Repository.SELF_EDITABLE`` — minus ``avatar_path``
    — are read, and the repository filters against that same set, so a field
    added to the form cannot write a column it was not meant to. Notably absent:
    ``role`` and ``status``. A client changing their own role is the whole
    privilege-escalation question, and it is answered by never reading it.
    """
    if (denied := _csrf_guard()) is not None:
        return denied

    user = current_user()
    repo = g.repo

    display_name = (request.form.get("display_name") or "").strip()[:128]
    email = (request.form.get("email") or "").strip()[:254]
    phone = (request.form.get("phone") or "").strip()[:32]
    country = (request.form.get("country") or "").strip()[:64]

    if not email:
        flash("An email address is required.", "danger")
        return redirect(url_for("profile.settings"))
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        flash("That does not look like an email address.", "danger")
        return redirect(url_for("profile.settings"))

    # An email identifies the account for sign-in and password recovery, so it
    # must not be claimable by two accounts at once: whoever holds it can
    # otherwise be sent the password-reset link for a different account.
    #
    # Checked here rather than left to the database because the column has no
    # unique index — it predates self-registration and older rows share an empty
    # string, so adding one would need a table rebuild. The gap that leaves is
    # small and worth naming: two simultaneous submissions of the same address
    # could both pass this check and both save. There is no money or access
    # attached to an address, and both accounts are the same person's in every
    # case that matters, so it is a duplication rather than a takeover.
    holder = repo.get_user_by_email(email)
    if holder is not None and holder.id != user.id:
        flash("That email address is already in use.", "danger")
        return redirect(url_for("profile.settings"))

    repo.update_user(user.id, display_name=display_name, email=email,
                     phone=phone, country=country)
    log.info("User %s updated their profile", user.id)
    flash("Your details have been saved.", "success")
    return redirect(url_for("profile.settings"))


@profile_bp.post("/settings/avatar")
@client_required
def avatar_upload():
    if (denied := _csrf_guard()) is not None:
        return denied

    user = current_user()
    repo = g.repo

    blob, error = _read_upload(request.files.get("avatar"))
    if error:
        flash(error, "danger")
        return redirect(url_for("profile.settings"))

    detected, error = validate_image(blob)
    if error:
        flash(error, "danger")
        return redirect(url_for("profile.settings"))

    extension = detected[0]
    try:
        name = store_avatar(user, blob, extension)
    except OSError:
        # Disk full, permissions, a read-only volume. The user gets a sentence;
        # the operator gets the traceback.
        log.exception("Could not store an avatar for user %s", user.id)
        flash("Your picture could not be saved. Please try again.", "danger")
        return redirect(url_for("profile.settings"))

    # The previous file is removed only after the replacement is on disk, so a
    # failure above leaves the old picture intact rather than a blank space.
    previous = user.avatar_path or ""
    repo.update_user(user.id, avatar_path=name)
    if previous and previous != name:
        discard_avatar(previous)

    flash("Your picture has been updated.", "success")
    return redirect(url_for("profile.settings"))


@profile_bp.post("/settings/avatar/remove")
@client_required
def avatar_remove():
    if (denied := _csrf_guard()) is not None:
        return denied

    user = current_user()
    previous = user.avatar_path or ""
    g.repo.update_user(user.id, avatar_path="")
    discard_avatar(previous)
    flash("Your picture has been removed.", "success")
    return redirect(url_for("profile.settings"))


@profile_bp.post("/settings/password")
@client_required
def password_change():
    """Change the signed-in user's own password.

    The current password is required even though the session already proves who
    this is: without it, anyone who reaches an unlocked browser — or who gets
    hold of a session cookie — can lock the owner out of their own account in
    one request. It is the one place a stolen session is worth less than the
    password itself.

    The new password goes through the same :func:`app.auth.password_problem`
    policy as registration, so the two cannot drift apart.
    """
    if (denied := _csrf_guard()) is not None:
        return denied

    user = current_user()
    current = request.form.get("current_password") or ""
    new = request.form.get("new_password") or ""
    confirm = request.form.get("confirm_password") or ""

    def fail(message: str):
        flash(message, "danger")
        return redirect(url_for("profile.settings"))

    if not verify_password(current, user.password_hash):
        # Deliberately vague and identical to the "no change made" outcome: a
        # precise message here would confirm a guessed password to someone
        # holding a stolen session, which is the case this check exists for.
        log.warning("Failed password change for user %s", user.id)
        return fail("Your current password is not correct.")

    if new != confirm:
        return fail("The two new passwords do not match.")

    if new == current:
        return fail("Your new password must be different from your current one.")

    if (problem := password_problem(new, minimum=_cfg().min_password_length)):
        return fail(problem)

    if not change_password(g.repo, user, new):
        log.error("Could not change the password for user %s", user.id)
        return fail("Your password could not be changed. Please try again.")

    log.info("User %s changed their own password", user.id)
    flash("Your password has been changed. Other devices have been signed out.",
          "success")
    return redirect(url_for("profile.settings"))


@profile_bp.get("/media/avatar/<filename>")
@client_required
def avatar(filename: str):
    """Stream a stored avatar.

    Not a static file, and not cacheable by a shared proxy. The filename is
    checked against the generated-name pattern before it is joined to the
    directory, and the response's content type comes from the file's own bytes
    via :func:`sniff_image` rather than from anything the uploader supplied. If
    the bytes are not a recognised image the file is not served at all — so even
    a file that reached the directory by some other route is inert here.

    A missing file answers 404 rather than an error, because a stale reference
    in a page that is already open is a normal thing to happen, not a fault.
    """
    if not _FILENAME_RE.match(filename):
        return Response(status=404)

    path = MEDIA_ROOT / filename
    if not path.is_file():
        return Response(status=404)

    try:
        with path.open("rb") as handle:
            head = handle.read(32)
    except OSError:
        log.warning("Could not read avatar %r", filename, exc_info=True)
        return Response(status=404)

    detected = sniff_image(head)
    if detected is None:
        return Response(status=404)

    _, content_type, _, _ = detected
    response = send_file(path, mimetype=content_type, conditional=True,
                         etag=True, max_age=300)
    # The content type above is asserted, not guessed: nosniff stops a browser
    # from overriding it by inspecting the bytes, which is what would let a
    # disguised file be treated as a document.
    response.headers["X-Content-Type-Options"] = "nosniff"
    # inline rather than attachment: this is displayed, not downloaded. Safe
    # because the type is one of the raster formats and nothing else can reach
    # this point.
    response.headers["Content-Disposition"] = "inline"
    # Per-user content behind a session cookie: a shared cache must not keep it.
    response.headers["Cache-Control"] = "private, max-age=300"
    return response


def register_profile(app) -> None:
    """Register the routes and make :func:`avatar_url` available to templates."""
    app.register_blueprint(profile_bp)
    # A template global rather than a filter: it needs the request context for
    # url_for, and `avatar_url(user)` reads better at the call site than
    # `user | avatar_url`.
    app.jinja_env.globals["avatar_url"] = avatar_url
