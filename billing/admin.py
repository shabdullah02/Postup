from datetime import timedelta

from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin
from django.utils import timezone

from billing.models import AdminAuditLog, Subscription


User = get_user_model()


class SubscriptionInline(admin.StackedInline):
    model = Subscription
    extra = 0
    max_num = 1
    can_delete = False


try:
    admin.site.unregister(User)
except admin.sites.NotRegistered:
    pass


@admin.register(User)
class BillingUserAdmin(UserAdmin):
    inlines = [SubscriptionInline]
    readonly_fields = UserAdmin.readonly_fields + ("is_active",)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        AdminAuditLog.objects.create(
            who=request.user,
            who_identifier=f"{request.user.pk}:{request.user.get_username()}",
            target=obj,
            target_identifier=f"{obj.pk}:{obj.get_username()}",
            action="user_updated" if change else "user_created_in_admin",
            details={"changed_fields": list(form.changed_data)},
        )

    def save_formset(self, request, form, formset, change):
        if formset.model is not Subscription:
            return super().save_formset(request, form, formset, change)

        previous = {
            item.pk: {
                "plan": item.plan,
                "status": item.status,
                "current_period_end": item.current_period_end.isoformat()
                if item.current_period_end else None,
            }
            for item in Subscription.objects.filter(user=form.instance)
        }
        changed = formset.save()
        for subscription in changed:
            before = previous.get(subscription.pk)
            after = {
                "plan": subscription.plan,
                "status": subscription.status,
                "current_period_end": subscription.current_period_end.isoformat()
                if subscription.current_period_end else None,
            }
            if before != after:
                AdminAuditLog.objects.create(
                    who=request.user,
                    who_identifier=f"{request.user.pk}:{request.user.get_username()}",
                    target=subscription.user,
                    target_identifier=f"{subscription.user_id}:{subscription.user.get_username()}",
                    action="subscription_updated_in_admin",
                    details={"before": before, "after": after},
                )

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "plan",
        "status",
        "current_period_end",
        "stripe_customer_id",
        "updated_at",
    )
    list_filter = ("plan", "status")
    search_fields = ("user__username", "user__email", "stripe_customer_id")
    actions = ("suspend_subscriptions", "activate_subscriptions", "extend_30_days")

    def save_model(self, request, obj, form, change):
        before = None
        if change:
            previous = Subscription.objects.get(pk=obj.pk)
            before = {
                "plan": previous.plan,
                "status": previous.status,
                "current_period_end": previous.current_period_end.isoformat()
                if previous.current_period_end else None,
            }
        super().save_model(request, obj, form, change)
        after = {
            "plan": obj.plan,
            "status": obj.status,
            "current_period_end": obj.current_period_end.isoformat()
            if obj.current_period_end else None,
        }
        AdminAuditLog.objects.create(
            who=request.user,
            who_identifier=f"{request.user.pk}:{request.user.get_username()}",
            target=obj.user,
            target_identifier=f"{obj.user_id}:{obj.user.get_username()}",
            action="subscription_updated_in_admin" if change else "subscription_created_in_admin",
            details={"before": before, "after": after},
        )

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Suspend selected subscriptions")
    def suspend_subscriptions(self, request, queryset):
        self._update_subscriptions(request, queryset, "suspend")

    @admin.action(description="Activate selected subscriptions")
    def activate_subscriptions(self, request, queryset):
        self._update_subscriptions(request, queryset, "activate")

    @admin.action(description="Extend selected subscriptions by 30 days")
    def extend_30_days(self, request, queryset):
        self._update_subscriptions(request, queryset, "extend")

    def _update_subscriptions(self, request, queryset, action):
        for subscription in queryset.select_related("user"):
            details = {"before_status": subscription.status}
            if action == "suspend":
                subscription.status = Subscription.Status.SUSPENDED
                audit_action = "subscription_suspended"
            elif action == "activate":
                subscription.status = Subscription.Status.ACTIVE
                audit_action = "subscription_activated"
            else:
                base = subscription.current_period_end or timezone.now()
                if base < timezone.now():
                    base = timezone.now()
                subscription.current_period_end = base + timedelta(days=30)
                details["current_period_end"] = subscription.current_period_end.isoformat()
                audit_action = "subscription_extended_30_days"
            subscription.save()
            AdminAuditLog.objects.create(
                who=request.user,
                who_identifier=f"{request.user.pk}:{request.user.get_username()}",
                target=subscription.user,
                target_identifier=f"{subscription.user_id}:{subscription.user.get_username()}",
                action=audit_action,
                details=details,
            )


@admin.register(AdminAuditLog)
class AdminAuditLogAdmin(admin.ModelAdmin):
    list_display = ("who", "target_identifier", "action", "timestamp")
    list_filter = ("action", "timestamp")
    search_fields = ("who__username", "target_identifier", "action")
    readonly_fields = ("who", "target", "target_identifier", "action", "timestamp", "details")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False