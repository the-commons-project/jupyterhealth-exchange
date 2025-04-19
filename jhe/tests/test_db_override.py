from django.conf import settings
from django.test import TestCase, override_settings

@override_settings(
    DATABASES={
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': ':memory:',
        }
    }
)
class DatabaseEngineTest(TestCase):
    def test_database_engine_is_sqlite(self):
        engine = settings.DATABASES["default"]["ENGINE"]
        self.assertEqual(engine, "django.db.backends.sqlite3")