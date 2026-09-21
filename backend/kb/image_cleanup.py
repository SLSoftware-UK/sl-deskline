"""
Housekeeping for orphaned inline markdown images.

Every drag-and-drop/paste upload in the markdown editor
(kb/views.py::markdown_image_upload) saves a file straight into
MARKDOWNX_MEDIA_PATH and inserts a reference to it into Article.body
as plain markdown text -- unlike ArticlePhoto, there's no ForeignKey
or other DB row tying that file to the article it was dropped into.
If an author later deletes the `![...](...)` line (or the whole
article), the file has no way of knowing it's no longer wanted and
just sits in storage forever: the media folder keeps growing even as
an author deletes images from an article in progress.

purge_orphaned_markdown_images() closes that gap: it scans every
Article.body (any org, any status -- a draft's images are just as
"in use" as a published one's) for markdownx image references, lists
every file actually sitting in MARKDOWNX_MEDIA_PATH, and deletes
whichever files aren't referenced anywhere. It's called after every
successful article create/edit/delete (kb/views.py) rather than run
as a separate scheduled job -- this is
one text scan over already-fetched article bodies plus one storage
directory listing, not a per-file network round trip, so doing it on
every save is cheap even as the KB grows.

It is nonetheless OPT-IN (settings.KB_PURGE_ORPHANED_IMAGES, default
False), because "not referenced by any Article here" is only the same
thing as "orphaned" once storage and the database belong to each other
-- which a fresh database pointed at storage that already has content
is not. See the guard in purge_orphaned_markdown_images below.

Filenames are compared, not full URLs. The URL django-markdownx's own
JS inserts is a snapshot from default_storage.url() taken at upload
time -- on S3-style storage with presigned URLs, should a deployment
ever configure one instead of the default FileSystemStorage, that URL
carries a presigned, time-limited query string
(AWS_QUERYSTRING_AUTH), which won't still match that same file's URL
computed later. Filenames survive that, because Django's storage
backends already guarantee them unique on write (AWS_S3_FILE_OVERWRITE
= False, FileSystemStorage's own equivalent default behaviour) -- see
the `photo_HinbMlC.jpg`-style names process_photo_image's uploads
already get. Comparing filenames only (not full relative paths, not
caring what markdown syntax variant wraps the URL) is deliberately
the more conservative direction to err in: a false negative (an
orphaned file that isn't cleaned up this pass) just gets caught next
save; a false positive (deleting a file something still points to)
would break a live article's image, so the matching here is loose on
purpose in the safe direction.
"""
import logging
import re

from django.conf import settings
from django.core.files.storage import default_storage
from markdownx.settings import MARKDOWNX_MEDIA_PATH

from .models import Article

logger = logging.getLogger(__name__)

# Matches the URL inside any markdown image reference, e.g.
# `![](/media/markdownx/photo_HinbMlC.jpg)` or the same followed by an
# attr_list size spec, `...jpg){: style="width: 100%" }` -- the size
# spec lives outside the `(...)`, so it never affects this match.
_IMAGE_REF_RE = re.compile(r'!\[[^\]]*\]\(([^)\s]+)')


def _referenced_filenames():
    """Every filename referenced by a markdown image reference in any
    article body, across every org and status. Deliberately not
    filtered to "looks like a markdownx URL" -- see module docstring
    on why over-matching here (protecting a file from deletion) is the
    safe direction to be wrong in, if it's ever wrong at all."""
    referenced = set()
    for body in Article.objects.exclude(body='').values_list('body', flat=True):
        for url in _IMAGE_REF_RE.findall(body):
            referenced.add(url.split('?', 1)[0].rsplit('/', 1)[-1])
    return referenced


def purge_orphaned_markdown_images():
    """Delete every file under MARKDOWNX_MEDIA_PATH that no
    Article.body references any more. Safe to call often -- a no-op
    when nothing's orphaned. Never raises: a storage hiccup should
    never break the article save/delete that triggered this, it just
    skips cleanup for that request (logged) and the next save tries
    again. Returns the number of files deleted, for an optional note
    in the caller's success message.

    OFF unless settings.KB_PURGE_ORPHANED_IMAGES says otherwise, and
    that setting defaults to False. The guard lives here rather than at
    the three call sites in kb/views.py so it cannot be forgotten at a
    fourth. Why it has to default off: "orphaned" is derived from
    Article.objects in *this* database, so against storage that already
    holds images this database never saw -- a previous install, or a
    directory shared with another site -- every one of those files
    under MARKDOWNX_MEDIA_PATH looks orphaned on the first save.
    That is not a risk worth carrying by default for a housekeeping
    nicety. See support_core/settings.py for the full reasoning and for
    when it is safe to turn on.
    """
    if not getattr(settings, 'KB_PURGE_ORPHANED_IMAGES', False):
        return 0

    referenced = _referenced_filenames()
    try:
        _dirs, files = default_storage.listdir(MARKDOWNX_MEDIA_PATH)
    except FileNotFoundError:
        # Nothing's ever been uploaded yet (fresh install, or a dev
        # environment with an empty media dir) -- not an error, just
        # nothing to clean up.
        return 0
    except Exception:
        logger.exception('purge_orphaned_markdown_images: could not list %s', MARKDOWNX_MEDIA_PATH)
        return 0

    deleted = 0
    for filename in files:
        if filename in referenced:
            continue
        try:
            default_storage.delete(MARKDOWNX_MEDIA_PATH + filename)
            deleted += 1
        except Exception:
            logger.exception('purge_orphaned_markdown_images: could not delete %s', filename)
    return deleted
