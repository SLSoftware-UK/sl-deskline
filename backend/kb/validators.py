"""
Shared validators for kb models.

The upload ceiling is `settings.MAX_PHOTO_UPLOAD_SIZE`, never a module
constant: both call sites (this validator, on ArticlePhoto.image, and
kb/views.py::markdown_image_upload for drag-and-drop inline images)
read it through max_photo_upload_bytes() below, so the documented env
var actually changes the enforced limit. Defaults to 25MB.
"""
from django.conf import settings
from django.core.exceptions import ValidationError

# Server-side ceiling for photo uploads, when nothing is configured.
# Generous headroom over anything a real article/showcase photo needs;
# it just protects the (2-worker) gunicorn service from being tied up
# decoding an enormous file.
DEFAULT_MAX_PHOTO_UPLOAD_BYTES = 25 * 1024 * 1024


def max_photo_upload_bytes():
    """The configured ceiling, read at call time.

    A function rather than a module-level constant on purpose: read once
    at import, `override_settings` in a test and a changed env var in
    a long-lived process would both be silently ignored.
    """
    return getattr(settings, 'MAX_PHOTO_UPLOAD_SIZE', DEFAULT_MAX_PHOTO_UPLOAD_BYTES)


def validate_image_size(value):
    max_bytes = max_photo_upload_bytes()
    if value.size > max_bytes:
        raise ValidationError(
            f'Image is too large ({value.size / 1024 / 1024:.1f}MB). '
            f'Maximum allowed is {max_bytes / 1024 / 1024:.0f}MB.'
        )
