from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        import core.signals  # noqa
        from core.jhe_settings_specs import register_core_settings

        # uscdi-patient-access: the module's settings keys
        from core.patient_access.settings import register_settings as register_patient_access_settings

        register_core_settings()
        register_patient_access_settings()  # uscdi-patient-access
