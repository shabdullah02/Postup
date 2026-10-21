"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import include, path
from accounts.views import SocialAccountsListView
from billing.admin_api import (
    AdminDeactivateUserView,
    AdminReactivateUserView,
    AdminStatsView,
    AdminSubscriptionUpdateView,
    AdminUserDetailView,
    AdminUsersView,
)
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/auth/', include('accounts.urls')),
    path('api/billing/', include('billing.urls')),
    path('api/admin/users/', AdminUsersView.as_view(), name='admin-users'),
    path('api/admin/users/<int:user_id>/subscription/', AdminSubscriptionUpdateView.as_view(), name='admin-user-subscription'),
    path('api/admin/users/<int:user_id>/deactivate/', AdminDeactivateUserView.as_view(), name='admin-user-deactivate'),
    path('api/admin/users/<int:user_id>/reactivate/', AdminReactivateUserView.as_view(), name='admin-user-reactivate'),
    path('api/admin/users/<int:user_id>/', AdminUserDetailView.as_view(), name='admin-user-detail'),
    path('api/admin/stats/', AdminStatsView.as_view(), name='admin-stats'),
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/', include('posts.urls')),
    path('api/', include('socials.urls')),
    path('api/social-accounts/', SocialAccountsListView.as_view(), name='social-accounts-list'),
]
