from rest_framework import serializers

from core.models import JheSetting

# Why value is JSONField?
# - Can handle "50", 50, true, "true", objects, arrays, etc.
# - Model method handles coercion based on value_type.

# Why resolved_value instead of value?
# - Because DRF can't have the same field name be both write-only and read-only cleanly.


class JheSettingSerializer(serializers.ModelSerializer):
    value = serializers.JSONField(write_only=True, required=False)
    resolved_value = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = JheSetting
        fields = [
            "id",
            "key",
            "value_type",
            "value",  # input
            "resolved_value",  # output
            "last_updated",
        ]

    def get_resolved_value(self, obj):
        return obj.get_value()

    def create(self, validated_data):
        value = validated_data.pop("value", None)
        value_type = validated_data.get("value_type")

        obj = JheSetting(**validated_data)
        if value is not None:
            obj.set_value(value_type, value)
        else:
            # If you require value on create, enforce here:
            raise serializers.ValidationError({"value": "This field is required."})

        obj.save()
        return obj

    def update(self, instance, validated_data):
        value = validated_data.pop("value", None)

        # allow changing type/value together
        new_value_type = validated_data.get("value_type", instance.value_type)

        for attr, v in validated_data.items():
            setattr(instance, attr, v)

        if value is not None:
            instance.set_value(new_value_type, value)
        elif "value_type" in validated_data:
            # If they changed type but didn't supply value, that's usually an error
            raise serializers.ValidationError({"value": "Required when changing value_type."})

        instance.save()
        return instance
