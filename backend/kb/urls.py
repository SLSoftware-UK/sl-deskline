from django.urls import path

from . import views

app_name = 'kb'

urlpatterns = [
    path('', views.article_list, name='article-list'),
    path('robots.txt', views.robots_txt, name='robots-txt'),

    # Authoring — must be registered before articles/<slug:slug>/ below,
    # otherwise "manage"/"new" would themselves be swallowed as slugs.
    path('articles/manage/', views.article_manage, name='article-manage'),
    path('articles/new/', views.article_create, name='article-create'),
    path('articles/<slug:slug>/edit/', views.article_edit, name='article-edit'),
    path('articles/<slug:slug>/delete/', views.article_delete, name='article-delete'),

    path('articles/<slug:slug>/', views.article_detail, name='article-detail'),
    path('articles/<slug:slug>/rate/', views.rate_article, name='article-rate'),

    # Category / Tag authoring.
    path('taxonomy/', views.taxonomy_manage, name='taxonomy-manage'),
    path('categories/new/', views.category_create, name='category-create'),
    path('categories/<int:pk>/edit/', views.category_edit, name='category-edit'),
    path('categories/<int:pk>/delete/', views.category_delete, name='category-delete'),
    path('tags/new/', views.tag_create, name='tag-create'),
    path('tags/<int:pk>/edit/', views.tag_edit, name='tag-edit'),
    path('tags/<int:pk>/delete/', views.tag_delete, name='tag-delete'),
]
