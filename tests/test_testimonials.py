"""The testimonial section is truthful or absent.

A quote in someone else's voice is a claim about the product, so the module and
the homepage are tested together: unpublished quotes are filtered out, and a
site with no quotes renders no testimonial section at all rather than a heading
with empty cards under it.
"""
from __future__ import annotations

from dataclasses import replace

from app.testimonials import Testimonial as Quote
from app.testimonials import published
from app.web import create_app
from config import get_settings

from test_web import _FakeJobs, _repo


def _anon(repo):
    settings = replace(get_settings(), flask_secret_key="test-secret",
                       telegram_enabled=False, telegram_bot_token="",
                       telegram_chat_id="", bootstrap_admin_username="",
                       bootstrap_admin_password="")
    app = create_app(settings=settings, repository=repo, setup_db=False,
                     jobs=_FakeJobs())
    return app.test_client()


def _home():
    resp = _anon(_repo()).get("/")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)


def test_an_unpermitted_quote_is_never_published():
    rows = (
        Quote(quote="Great product.", name="A Customer"),
        Quote(quote="Great product.", name="A Customer", permitted=True),
        Quote(quote="   ", name="A Customer", permitted=True),
        Quote(quote="Great product.", name="   ", permitted=True),
    )
    assert [t.name for t in published(rows)] == ["A Customer"]
    assert [t.quote for t in published(rows)] == ["Great product."]


def test_the_homepage_omits_the_section_when_nothing_is_published():
    body = _home()
    assert "What customers say" not in body
    assert 'id="testimonials"' not in body


def test_the_homepage_renders_a_permitted_quote(monkeypatch):
    from app import testimonials as mod

    monkeypatch.setattr(mod, "TESTIMONIALS", (
        Quote(quote="I stopped watching four sessions a day.",
              name="A Customer", role="Premium member", permitted=True),
    ))

    body = _home()

    assert "What customers say" in body
    assert "I stopped watching four sessions a day." in body
    assert "A Customer" in body
    assert "Premium member" in body
