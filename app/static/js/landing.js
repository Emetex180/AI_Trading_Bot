/* Public marketing site: the collapsing navigation, the header's scrolled
 * state, and the section reveals.
 *
 * Every one of these is an enhancement, and the page is built to be complete
 * without them. base.html renders the navigation as a row that scrolls sideways
 * when there is no script, and the reveal styles in 3rader.css are scoped to
 * `.js` — a class set by a single line in the document head — so a visitor with
 * scripting disabled, a crawler, or a locked-down browser gets the whole page
 * and both calls to action rather than a menu that never opens.
 *
 * Loaded by public/base.html, which is the shell for the homepage, the pricing
 * and features pages and the legal documents.
 */
(function () {
  'use strict';

  var MOBILE = '(max-width: 860px)';

  // --- The collapsing navigation -----------------------------------------
  // Only reachable on a narrow screen *and* with scripting on: the toggle is
  // hidden by CSS until the `.js` class is present, so the button can never
  // appear without a handler behind it.
  var nav = document.querySelector('.mk-nav');
  var toggle = document.querySelector('[data-nav-toggle]');
  var menu = document.getElementById('mk-nav-menu');

  function setOpen(open) {
    if (!toggle || !menu) return;
    menu.classList.toggle('is-open', open);
    toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
    // The label describes what pressing the control will do next, so it reads
    // correctly before the press as well as after it.
    toggle.setAttribute('aria-label', open ? 'Close menu' : 'Open menu');
  }

  if (toggle && menu) {
    toggle.addEventListener('click', function () {
      setOpen(!menu.classList.contains('is-open'));
    });

    // A link inside the panel navigates, and the panel must not still be open
    // when the reader arrives.
    menu.addEventListener('click', function (event) {
      var link = event.target && event.target.closest ? event.target.closest('a') : null;
      if (link) setOpen(false);
    });

    document.addEventListener('keydown', function (event) {
      if (event.key !== 'Escape') return;
      if (!menu.classList.contains('is-open')) return;
      setOpen(false);
      // Focus goes back to the control that opened it, not to the top of the
      // document, so the next Tab continues from where the reader was.
      toggle.focus();
    });

    // Widening past the breakpoint restores the inline row, so the panel's
    // open state is dropped rather than left to reappear on the next resize.
    if (window.matchMedia) {
      var wide = window.matchMedia('(min-width: 861px)');
      var onChange = function (event) {
        if (event.matches) setOpen(false);
      };
      if (wide.addEventListener) wide.addEventListener('change', onChange);
      else if (wide.addListener) wide.addListener(onChange);
    }
  }

  // --- Header state ------------------------------------------------------
  // The header only gains its heavier background once the page has moved, so it
  // stays transparent over the hero wash at the top of the page.
  if (nav) {
    var ticking = false;
    var apply = function () {
      nav.classList.toggle('is-scrolled', window.pageYOffset > 8);
      ticking = false;
    };
    apply();
    window.addEventListener('scroll', function () {
      if (ticking) return;
      ticking = true;
      window.requestAnimationFrame(apply);
    }, { passive: true });
  }

  // --- Section reveals ---------------------------------------------------
  // Each section is marked once and then unobserved: re-animating on the way
  // back up is movement for its own sake.
  var reveals = document.querySelectorAll('.mk-reveal');
  if (reveals.length) {
    var reduced = window.matchMedia
      ? window.matchMedia('(prefers-reduced-motion: reduce)').matches
      : false;

    if (reduced || !('IntersectionObserver' in window)) {
      // Nothing to animate, or no way to know when to: show everything rather
      // than leaving it at the opacity the stylesheet gives a hidden reveal.
      for (var i = 0; i < reveals.length; i += 1) {
        reveals[i].classList.add('is-in');
      }
    } else {
      var observer = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;
          entry.target.classList.add('is-in');
          observer.unobserve(entry.target);
        });
      }, { rootMargin: '0px 0px -10% 0px', threshold: 0.05 });

      for (var j = 0; j < reveals.length; j += 1) {
        observer.observe(reveals[j]);
      }
    }
  }
})();
