from rest_framework import serializers

from core.models import JheUser, Organization, PractitionerOrganization


class PractitionerOrganizationSerializer(serializers.ModelSerializer):
    class Meta:
        model = PractitionerOrganization
        fields = ["id", "organization", "practitioner", "role"]
        depth = 1


class PatientOrganizationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ["id", "organization", "patient"]
        depth = 1


class OrganizationSerializer(serializers.ModelSerializer):
    current_user_role = serializers.SerializerMethodField()

    def to_representation(self, instance):
        self.fields["children"] = OrganizationSerializer(many=True, read_only=True)
        return super().to_representation(instance)

    class Meta:
        model = Organization
        fields = ["id", "name", "type", "part_of", "current_user_role"]

    def get_current_user_role(self, obj):
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            if request.user.is_superuser:
                return "super_user"
            try:
                practitioner = request.user.practitioner_profile
            except AttributeError:
                return None

            link = PractitionerOrganization.objects.filter(practitioner=practitioner, organization=obj).first()
            return link.role if link else None
        return None


class OrganizationWithoutLineageSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ["id", "name", "type"]


class OrganizationUsersSerializer(serializers.ModelSerializer):
    role = serializers.SerializerMethodField()

    class Meta:
        model = JheUser
        fields = ["id", "email", "first_name", "last_name", "role"]

    def get_role(self, user):
        if org_id := self.context.get("organization_id"):
            link = PractitionerOrganization.objects.filter(practitioner__jhe_user=user, organization_id=org_id).first()
            return link.role if link else None
        return None
