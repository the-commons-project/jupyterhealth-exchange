from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from core.models import JheSetting
from core.services.jhe_settings import SettingValueType, get_spec

# Why value is JSONField?
# - Can handle "50", 50, true, "true", objects, arrays, etc.
# - Model method handles coercion based on value_type.

# Why resolved_value instead of value?
# - Because DRF can't have the same field name be both write-only and read-only cleanly.

_VALUE_COLUMNS = frozenset(t.column for t in SettingValueType)


def _api_errors(error: DjangoValidationError) -> dict[str, list[str]]:
    """Model validation errors are keyed by column (value_int, value_string...) so the Django admin
    form can place them; the API has one `value` input, so fold the typed columns onto it."""
    if not hasattr(error, "error_dict"):
        return {"non_field_errors": error.messages}
    folded: dict[str, list[str]] = {}
    for field, messages in error.message_dict.items():
        folded.setdefault("value" if field in _VALUE_COLUMNS else field, []).extend(messages)
    return folded


class JheSettingSerializer(serializers.ModelSerializer):
    value = serializers.JSONField(write_only=True, required=False)
    resolved_value = serializers.SerializerMethodField(read_only=True)
    help = serializers.SerializerMethodField(read_only=True)
    example = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = JheSetting
        fields = [
            "id",
            "key",
            "value_type",
            "value",  # input
            "resolved_value",  # output
            "help",  # from the registry, null for unregistered keys
            "example",
            "last_updated",
        ]

    def get_resolved_value(self, obj):
        return obj.get_value()

    def get_help(self, obj) -> str | None:
        spec = get_spec(obj.key)
        return spec.help if spec else None

    def get_example(self, obj) -> str | None:
        spec = get_spec(obj.key)
        return spec.example if spec else None

    @staticmethod
    def _apply(obj: JheSetting, value_type: str, value) -> None:
        try:
            if value is not None:
                obj.set_value(value_type, value)
            obj.full_clean()
        except DjangoValidationError as e:
            raise serializers.ValidationError(_api_errors(e))

    def create(self, validated_data):
        value = validated_data.pop("value", None)
        value_type = validated_data.get("value_type")

        obj = JheSetting(**validated_data)
        if value is None:
            # If you require value on create, enforce here:
            raise serializers.ValidationError({"value": "This field is required."})
        self._apply(obj, value_type, value)

        obj.save()
        return obj

    def update(self, instance, validated_data):
        value = validated_data.pop("value", None)

        # allow changing type/value together
        new_value_type = validated_data.get("value_type", instance.value_type)

        for attr, v in validated_data.items():
            setattr(instance, attr, v)

        if value is None and "value_type" in validated_data:
            # If they changed type but didn't supply value, that's usually an error
            raise serializers.ValidationError({"value": "Required when changing value_type."})
        self._apply(instance, new_value_type, value)

        instance.save()
        return instance
