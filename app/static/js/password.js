/* Show / hide control for password fields.
 *
 * Wires every `[data-pw-toggle]` button rendered by the `password_field` macro
 * in _macros.html. One implementation serves all of them — the sign-in,
 * registration and reset pages, the account settings form and the two admin
 * forms — so the behaviour cannot drift between them.
 *
 * Delegated from the document rather than bound per button: the admin area
 * builds some forms after first paint, and a per-element binding would silently
 * miss those. It also means the script has no load-order requirement, so each
 * shell can load it wherever its other scripts go.
 *
 * Loaded by every shell that can render a password field: auth_base.html and
 * client/base.html (the admin area extends the latter).
 */
(function () {
  'use strict';

  function toggle(button) {
    var wrapper = button.closest('.pw-field');
    if (!wrapper) return;
    var input = wrapper.querySelector('input');
    if (!input) return;

    // The button always describes what pressing it will do, not what the field
    // currently is, so the label reads correctly before and after a press.
    var reveal = input.type === 'password';
    input.type = reveal ? 'text' : 'password';
    wrapper.classList.toggle('is-revealed', reveal);

    var label = reveal ? 'Hide password' : 'Show password';
    button.setAttribute('aria-pressed', reveal ? 'true' : 'false');
    button.setAttribute('aria-label', label);
    button.title = label;
  }

  document.addEventListener('click', function (event) {
    var target = event.target;
    // The click may land on the button or on one of the two SVGs inside it.
    var button = target && target.closest ? target.closest('[data-pw-toggle]') : null;
    if (!button) return;
    event.preventDefault();
    toggle(button);
  });
})();
