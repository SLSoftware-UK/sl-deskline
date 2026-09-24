"""
YouTube support for Article's optional supporting video.

Deliberately a dedicated field on Article (see Article.youtube_video_id),
not a shortcode or raw <iframe> inside the Markdown body: the body goes
through bleach (kb/markdown_utils.py), which strips iframes outright,
and keeping it that way means an author can never smuggle an arbitrary
frame into an article. Only an 11-character video ID is ever stored,
and every URL the templates emit is built from that ID here, so the
only hosts involved are the fixed ones below -- which is also exactly
what the CSP in help_core/security_middleware.py allows:

  - frame-src  https://www.youtube-nocookie.com  (embedded player)
  - img-src    https://i.ytimg.com               (thumbnail mode)

youtube-nocookie.com is YouTube's privacy-enhanced embed domain: no
tracking cookies are set until the viewer actually presses play.

Authors paste whatever they have -- a watch URL, a youtu.be share
link, a Shorts link, an embed URL, or the bare ID -- and
YouTubeVideoIdFormField normalises it to the ID before it's saved. That
lives on the *model* field's formfield(), so the in-app editor
(kb/forms.py::ArticleForm) and Django Admin both get it for free.
"""
import re
from urllib.parse import parse_qs, urlparse

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models

VIDEO_ID_RE = re.compile(r'^[A-Za-z0-9_-]{11}$')

_WATCH_HOSTS = {'youtube.com', 'm.youtube.com', 'music.youtube.com'}
_PATH_ID_HOSTS = _WATCH_HOSTS | {'youtube-nocookie.com'}
# /shorts/<id>, /embed/<id>, /live/<id>, /v/<id>
_PATH_PREFIXES = ('shorts', 'embed', 'live', 'v')


def extract_video_id(value):
    """
    Return the 11-character YouTube video ID from a URL or bare ID, or
    None if `value` isn't recognisably a YouTube video.

    :param value: Anything an author might paste.
    :type value: str
    :rtype: str | None
    """
    value = (value or '').strip()
    if not value:
        return None
    if VIDEO_ID_RE.match(value):
        return value

    if '://' not in value:
        value = 'https://' + value
    parsed = urlparse(value)
    if parsed.scheme not in ('http', 'https'):
        return None
    host = (parsed.hostname or '').lower()
    if host.startswith('www.'):
        host = host[4:]

    candidate = None
    parts = [p for p in parsed.path.split('/') if p]
    if host == 'youtu.be':
        candidate = parts[0] if parts else None
    elif host in _WATCH_HOSTS and parts[:1] == ['watch']:
        candidate = (parse_qs(parsed.query).get('v') or [None])[0]
    elif host in _PATH_ID_HOSTS and len(parts) >= 2 and parts[0] in _PATH_PREFIXES:
        candidate = parts[1]

    if candidate and VIDEO_ID_RE.match(candidate):
        return candidate
    return None


def embed_url(video_id):
    return f'https://www.youtube-nocookie.com/embed/{video_id}'


def watch_url(video_id):
    return f'https://www.youtube.com/watch?v={video_id}'


def thumbnail_url(video_id):
    # hqdefault (480x360) exists for every video; maxresdefault doesn't.
    # It's 4:3 with letterbox bars on 16:9 videos -- help.css crops it
    # with object-fit: cover inside a 16:9 box.
    return f'https://i.ytimg.com/vi/{video_id}/hqdefault.jpg'


class YouTubeVideoIdFormField(forms.CharField):
    """Accepts a YouTube URL or bare ID; cleans to the bare ID ('' if
    left blank)."""

    default_error_messages = {
        'invalid': 'That doesn’t look like a YouTube video link. Paste the '
                   'video’s URL (e.g. https://www.youtube.com/watch?v=… '
                   'or https://youtu.be/…) or its 11-character ID.',
    }

    def to_python(self, value):
        value = super().to_python(value)
        if not value:
            return ''
        video_id = extract_video_id(value)
        if video_id is None:
            raise ValidationError(self.error_messages['invalid'], code='invalid')
        return video_id


class YouTubeVideoIdField(models.CharField):
    """Stores a bare 11-character YouTube video ID (or '')."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault('max_length', 11)
        super().__init__(*args, **kwargs)
        self.validators.append(RegexValidator(
            VIDEO_ID_RE, 'Must be an 11-character YouTube video ID.',
        ))

    def formfield(self, **kwargs):
        # max_length=None: the *form* input must accept a full pasted
        # URL (well over 11 chars) -- it's only the cleaned ID that has
        # to fit the column, and the model validator still checks that.
        return super().formfield(**{
            'form_class': YouTubeVideoIdFormField,
            'max_length': None,
            **kwargs,
        })
