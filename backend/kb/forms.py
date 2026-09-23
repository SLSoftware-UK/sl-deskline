"""
ArticleForm — the in-app authoring form behind the new "New Article"
button (kb/views.py: article_create, article_edit), replacing the old
redirect straight into Django Admin's raw model-add form. Superuser-
only for now, gated by kb/decorators.py.

Deliberately does NOT expose org_id, author_user_id/author_display_name
or slug: this form only ever authors into the site's own public KB
(org_id=None, matching kb.views.PLATFORM_ORG_ID), authorship is set
server-side from the logged-in user (request.user.id — see views.py's
article_create),
and slug is left to Article.save()'s existing auto-slug-from-title
behaviour so editing a published article's title never changes its
live URL out from under an existing inbound link. ArticleStep/
ArticlePhoto inlines also aren't here yet — those still go through
Django Admin (kb.admin.ArticleAdmin) for now; a bespoke UI for them is
future work, this pass is specifically about replacing Markdown body
authoring.
"""
from django import forms

from .models import Article, Category, Tag, KBSettings


class ArticleForm(forms.ModelForm):
    class Meta:
        model = Article
        fields = ['title', 'category', 'tags', 'sort_order', 'summary', 'body', 'meta_description', 'status']
        widgets = {
            'tags': forms.SelectMultiple(attrs={'size': 6}),
            'summary': forms.Textarea(attrs={'rows': 3}),
            'meta_description': forms.TextInput(),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Scoped to the site's own public KB — see module docstring. A
        # private org KB would need its own form instance scoped to
        # that org's own Category/Tag rows once that authoring UI exists.
        self.fields['category'].queryset = Category.objects.filter(org_id=None)
        self.fields['tags'].queryset = Tag.objects.filter(org_id=None)
        self.fields['category'].empty_label = None if self.fields['category'].queryset.exists() else 'No categories yet'
        self.fields['title'].widget.attrs.setdefault('autofocus', True)
        self.fields['meta_description'].widget.attrs.setdefault('maxlength', 160)


class CategoryForm(forms.ModelForm):
    """Backs kb/views.py's taxonomy_manage/category_create/category_edit
    — the same "had to add it straight into the DB" gap the article
    editor closed for Article itself, now closed for Category too.

    org_id is deliberately NOT a field here (an earlier version of this
    form exposed it as a free-text number input — wrong: that let
    whoever's signed in type in *any* org's id, which is exactly the
    "one org writing articles into another org's KB" hole).
    Same treatment as ArticleForm: org_id is set server-side from the
    logged-in user, never from user input — see kb/views.py's
    _authoring_org_id and category_create/category_edit."""
    class Meta:
        model = Category
        fields = ['name', 'visibility', 'sort_order']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['name'].widget.attrs.setdefault('autofocus', True)
        # The model field is non-null with a 0 default, which would
        # otherwise make this a required input on the inline "Add
        # category" form. Optional here, defaulting to 0, so adding a
        # category stays a two-field job.
        self.fields['sort_order'].required = False

    def clean_sort_order(self):
        return self.cleaned_data.get('sort_order') or 0


class TagForm(forms.ModelForm):
    """Same rationale as CategoryForm, for Tag — see its docstring."""
    class Meta:
        model = Tag
        fields = ['name']

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['name'].widget.attrs.setdefault('autofocus', True)


class KBSettingsForm(forms.ModelForm):
    class Meta:
        model = KBSettings
        fields = ['ratings_enabled', 'show_helpful_count']
