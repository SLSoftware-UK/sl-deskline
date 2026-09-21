/**
 * Fixes django-markdownx's own upload-error UX, without patching its
 * vendored markdownx.js: on any upload failure, that library's client
 * code inserts the literal, unhelpful text "Invalid response" into the
 * editor at the cursor and only logs the real reason to the browser
 * console — see kb/views.py::markdown_image_upload's docstring for the
 * full story (this was surfaced by a 14MB photo upload appearing to
 * fail for no visible reason).
 *
 * markdownx *does* dispatch a `markdownx.fileUploadError` CustomEvent
 * on the relevant `.markdownx` element, with the parsed JSON response
 * as `event.detail[0]` — our own kb/views.py::markdown_image_upload
 * always returns `{"error": "<human-readable message>"}` on failure,
 * so we can show that instead. The event fires synchronously just
 * before the library inserts its placeholder text, so the removal
 * below runs on the next tick (setTimeout 0) once that insertion has
 * actually happened, precisely at the cursor offset captured when the
 * event fired (not a blind find-and-replace, in case "Invalid
 * response" is ever legitimately part of someone's article text).
 *
 * No dependency, vanilla JS — matches this app's no-build-step
 * approach.
 */
(function () {
  var PLACEHOLDER = 'Invalid response';

  function setUp(container) {
    var editor = container.querySelector('.markdownx-editor');
    if (!editor) { return; }

    var banner = document.createElement('p');
    banner.className = 'field-error markdownx-upload-error';
    banner.hidden = true;
    container.insertAdjacentElement('afterend', banner);

    container.addEventListener('markdownx.fileUploadError', function (event) {
      var detail = event.detail && event.detail[0];
      var message = (detail && detail.error) || 'That upload failed. Please try a different image.';
      var insertAt = editor.selectionStart;

      setTimeout(function () {
        if (editor.value.slice(insertAt, insertAt + PLACEHOLDER.length) === PLACEHOLDER) {
          editor.value = editor.value.slice(0, insertAt) + editor.value.slice(insertAt + PLACEHOLDER.length);
          editor.selectionStart = editor.selectionEnd = insertAt;
        }
        banner.textContent = message;
        banner.hidden = false;
      }, 0);
    });

    // A successful upload replaces whatever error is currently shown —
    // otherwise a stale message could sit there after the visitor just
    // fixes it and drops a working image in.
    //
    // We also append a `{: style="width: 100%" }` size spec right after
    // the image markdown markdownx just inserted. Without it every
    // dropped image renders at its natural pixel size (capped only by
    // the `max-width: 100%` safety net in help.css), which on a modern
    // phone photo is usually much wider than the article column and
    // looks inconsistent article to article. Defaulting to 100% makes
    // images fill the column and scale cleanly across screen sizes;
    // authors can still edit or delete that `{: ... }` block by hand
    // for a deliberately narrower image (see the field hint below).
    //
    // `event.detail[0]` is the same JSON our kb/views.py::markdown_image_upload
    // view returned, so `detail.image_code` is the exact string
    // markdownx's own insertImage() just spliced in at the cursor. We
    // only proceed if the text immediately before the cursor still
    // matches it exactly — a cheap guard against ever mangling
    // something else if this library's internals change under us.
    container.addEventListener('markdownx.fileUploadEnd', function (event) {
      banner.hidden = true;

      var detail = event.detail && event.detail[0];
      var code = detail && detail.image_code;
      if (!code) { return; }

      var SIZE_SUFFIX = '{: style="width: 100%" }';
      var insertAt = editor.selectionStart;
      var justInserted = editor.value.slice(insertAt - code.length, insertAt);
      if (justInserted !== code) { return; }

      editor.value = editor.value.slice(0, insertAt) + SIZE_SUFFIX + editor.value.slice(insertAt);
      editor.selectionStart = editor.selectionEnd = insertAt + SIZE_SUFFIX.length;

      // Re-trigger markdownx's own 'input' listener so the live preview
      // and editor height pick up the appended size spec — insertImage()
      // already fired this once for the bare image_code, before we
      // added our suffix.
      editor.dispatchEvent(new Event('input', { bubbles: true }));
    });
  }

  document.querySelectorAll('.markdownx').forEach(setUp);
}());
