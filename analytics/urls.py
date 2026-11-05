from django.urls import path

from .views import PageOverviewView, PagePostsView

urlpatterns = [
    path("overview/", PageOverviewView.as_view(), name="analytics-overview"),
    path("posts/", PagePostsView.as_view(), name="analytics-posts"),
]
