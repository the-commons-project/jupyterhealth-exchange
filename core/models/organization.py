from django.db import models
from django.db.models import Q

from .patient import PatientOrganization
from .practitioner import PractitionerOrganization


class Organization(models.Model):
    # https://build.fhir.org/valueset-organizations-type.html
    ORGANIZATION_TYPES = {
        "root": "ROOT",
        "prov": "Healthcare Provider",
        "dept": "Hospital Department",
        "team": "Organizational team",
        "govt": "Government",
        "ins": "Insurance Company",
        "pay": "Payer",
        "edu": "Educational Institute",
        "reli": "Religious Institution",
        "crs": "Clinical Research Sponsor",
        "cg": "Community Group",
        "bus": "Non-Healthcare Business or Corporation",
        "other": "Other",
        "laboratory": "Laboratory",
        "imaging": "Imaging Center",
        "pharmacy": "Pharmacy",
        "health-information-network": "Health Information Network",
        "health-data-aggregator": "Health Data Aggregator",
    }

    name = models.CharField()
    type = models.CharField(choices=list(ORGANIZATION_TYPES.items()), null=False, blank=False)
    part_of = models.ForeignKey("self", on_delete=models.CASCADE, null=True, blank=True)
    last_updated = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name or f"Organization {self.pk}"

    # Helper method to return all users in this organization
    @property
    def users(self):
        from .jhe_user import JheUser  # lazy import to avoid circular with jhe_user → organization

        patient_user_ids = (
            PatientOrganization.objects.filter(organization=self)
            .select_related("patient__jhe_user")
            .values_list("patient__jhe_user_id", flat=True)
        )

        practitioner_user_ids = (
            PractitionerOrganization.objects.filter(organization=self)
            .select_related("practitioner__jhe_user")
            .values_list("practitioner__jhe_user_id", flat=True)
        )

        # Combine the IDs and get all of the users
        return JheUser.objects.filter(Q(id__in=patient_user_ids) | Q(id__in=practitioner_user_ids))

    @staticmethod
    def collect_children(parent):
        children = Organization.get_children(parent.id)
        for child in children:
            parent.children.append(child)
            Organization.collect_children(child)

    @staticmethod
    def get_children(parent_id):
        return Organization.objects.filter(part_of=parent_id).order_by("name")

    @staticmethod
    def for_practitioner(practitioner_user_id):
        # Return the organizations the practitioner identified by practitioner_user_id belongs
        # to. The traversal walks Organization -> PractitionerOrganization -> Practitioner ->
        # JheUser via the "practitioners" reverse relation (which spans the
        # PractitionerOrganization join table), so an organization matches only when the
        # practitioner is one of its members.
        return Organization.objects.filter(practitioners__jhe_user_id=practitioner_user_id)

    @staticmethod
    def for_patient(patient_user_id):
        # Return the organizations the patient identified by patient_user_id belongs to. The
        # traversal walks Organization -> PatientOrganization -> Patient -> JheUser via the
        # "patients" reverse relation (which spans the PatientOrganization join table), so an
        # organization matches only when the patient is one of its members.
        return Organization.objects.filter(patients__jhe_user_id=patient_user_id)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.children = []
