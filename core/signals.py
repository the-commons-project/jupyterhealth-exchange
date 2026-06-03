# accounts/signals.py

from django.contrib.auth import get_user_model
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from oauth2_provider.models import get_application_model

from core.models import JheClient, Practitioner

User = get_user_model()
Application = get_application_model()


@receiver(pre_save, sender=User)
def before_superuser_created(sender, instance, **kwargs):
    if instance._state.adding and instance.is_superuser and not instance.user_type:
        print(f"signals pre_save: superuser {instance.email} - setting user_type=practitioner")
        instance.user_type = "practitioner"


@receiver(post_save, sender=User)
def on_superuser_created(sender, instance, created, **kwargs):
    if created and instance.is_superuser:
        print(f"signals post_save: superuser {instance.email} - adding Practitioner")
        Practitioner.objects.create(jhe_user=instance)


@receiver(post_save, sender=Application)
def on_application_created(sender, instance, created, **kwargs):
    if created:
        JheClient.objects.get_or_create(application=instance)
