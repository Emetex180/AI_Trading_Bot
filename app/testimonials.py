"""Customer testimonials shown on the marketing site.

Only quotes we can stand behind belong in here. A testimonial is a claim about
the product made in someone else's voice, so the rules are stricter than for our
own copy:

* **The words are the customer's.** Not a paraphrase, not a composite, not a
  plausible-sounding quote written to fill a grid.
* **The attribution is real and permitted.** A name we can use, and that person's
  agreement to publish it on the site.
* **A source is recorded.** Where the quote came from — an email, a support
  thread, a review — so that if it is ever questioned we can produce the record.

The tuple below is deliberately empty. The homepage renders the testimonial
section only when it holds at least one permitted entry, so launching without
testimonials looks like a finished page, not a page with a placeholder. Never
add an invented quote here to make the section appear.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Testimonial:
    """One published quote, in the customer's own words."""

    #: The quote exactly as the customer wrote or approved it.
    quote: str
    #: How the customer is credited — name, or a name they asked us to use.
    name: str
    #: Plan or role context, if the customer agreed to share it.
    role: str = ""
    #: Where the quote came from, for our records (not rendered on the page).
    source: str = ""
    #: The customer's permission to publish. Nothing renders without it.
    permitted: bool = False


#: Published testimonials, newest first. Empty until real, permitted quotes
#: exist; see the module docstring.
TESTIMONIALS: tuple[Testimonial, ...] = ()


def published(testimonials: tuple[Testimonial, ...] | None = None
              ) -> tuple[Testimonial, ...]:
    """The subset safe to render: permitted, with a quote and an attribution.

    ``None`` reads :data:`TESTIMONIALS` at call time, so a test (or a future
    admin-provided source) can replace the module constant without the function
    holding a stale default.
    """
    if testimonials is None:
        testimonials = TESTIMONIALS
    return tuple(t for t in testimonials
                 if t.permitted and t.quote.strip() and t.name.strip())
