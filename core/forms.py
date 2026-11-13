from django import forms
from django.contrib.auth import get_user_model

from core.services.jhe_settings import get_setting

User = get_user_model()


class UserRegistrationForm(forms.ModelForm):
    password = forms.CharField(label="Password")

    class Meta:
        model = User
        fields = ["email", "password"]

    def clean(self, *args, **kwargs):
        email = self.cleaned_data.get("email")
        password = self.cleaned_data.get("password")
        email_check = User.objects.filter(email=email)
        submitted = self.data.get("joincode")
        if email_check.exists():
            raise forms.ValidationError("This Email already exists")
        if len(password) < 5:
            raise forms.ValidationError("Your Password should have more than 5 characters")
        # Closed until an admin sets the code: with no code configured, nothing matches, not
        # even a request that omits the field (both sides would be None).
        configured = get_setting("site.registration_invite_code")
        if not configured:
            raise forms.ValidationError("Registration is closed: no invite code has been set.")
        if not submitted or submitted != configured:
            raise forms.ValidationError("Your Invite Code is invalid.")
        return super().clean(*args, **kwargs)
