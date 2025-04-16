import os
from .settings import *

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ.get("DB_NAME", "test_jhe_dev"),
        'USER': os.environ.get("DB_USER", "postgres"),
        'PASSWORD': os.environ.get("DB_PASSWORD", "postgres"),
        'HOST': os.environ.get("DB_HOST", "localhost"),
        'PORT': os.environ.get("DB_PORT", "5432"),
    }
}
# Disable migrations for the oauth2_provider app to speed up tests
# This is a common practice in testing to avoid running migrations on the test database.
# It assumes that the app is already migrated in the main database.
MIGRATION_MODULES = {
    'oauth2_provider': None,
}

TEST_RUNNER = 'django.test.runner.DiscoverRunner'