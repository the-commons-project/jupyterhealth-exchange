# accounts/signals.py

import logging

from allauth.socialaccount.models import SocialApp
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

from core.context_processors import SAML2_SINGLE_SOCIAL_APP_CACHE_KEY
from core.models import JheSetting, Practitioner
from core.services.jhe_settings import setting_cache_key

User = get_user_model()
logger = logging.getLogger(__name__)


@receiver(post_save, sender=JheSetting)
@receiver(post_delete, sender=JheSetting)
def clear_jhe_setting_cache(sender, instance, **kwargs):
    # get_setting() caches each key for DEFAULT_CACHE_TTL. Dropping the entry on save makes an
    # operator's change visible at once in the process that saved it; with the default
    # per-process LocMemCache, other workers still wait out the TTL. A row whose key was renamed
    # leaves the old key's entry to expire on its own.
    cache.delete(setting_cache_key(instance.key))


@receiver(post_save, sender=SocialApp)
@receiver(post_delete, sender=SocialApp)
def clear_saml2_social_app_cache(sender, **kwargs):
    # The SAML login-button gate caches the "exactly one SAML SocialApp" count
    # (context_processors._saml2_enabled). Adding or removing an IdP row must
    # flip the button at once — this is the setup guide's back-out step — rather
    # than waiting out the cache TTL.
    cache.delete(SAML2_SINGLE_SOCIAL_APP_CACHE_KEY)


@receiver(pre_save, sender=User)
def before_superuser_created(sender, instance, **kwargs):
    if instance._state.adding and instance.is_superuser and not instance.user_type:
        logger.debug("pre_save: superuser %s - setting user_type=practitioner", instance.email)
        instance.user_type = "practitioner"


@receiver(post_save, sender=User)
def on_superuser_created(sender, instance, created, **kwargs):
    if created and instance.is_superuser:
        logger.debug("post_save: superuser %s - adding Practitioner", instance.email)
        Practitioner.objects.create(jhe_user=instance)
