import logging
import urllib

import jwt
import requests
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth import login
from django.contrib.auth import logout as django_logout
from django.contrib.auth.views import LoginView as BaseLoginView
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode

from ..forms import UserRegistrationForm
from ..tokens import account_activation_token

logger = logging.getLogger(__name__)

User = get_user_model()

def home(request):
    return render(request, 'home/home.html')

class LoginView(BaseLoginView):
    def post(self, request, *args, **kwargs):
        username = self.request.POST.get('username', '').strip()
        domain = username.split("@")[-1] if '@' in username else ''

        if domain in settings.SSO_VALID_DOMAINS:
            return redirect(reverse("saml_signin"))
        return super().post(request, *args, **kwargs)

def logout(request):
    django_logout(request)
    return redirect('home')

def profile(request):
    return redirect('/portal/')

def client_auth_callback(request):
    return render(request, 'client/client_auth/callback.html')

def client_auth_callback_popup(request):
    return render(request, 'client/client_auth/callback_popup.html')

def client_auth_login(request):
    return render(request, 'client/client_auth/login.html')

def signup(request):
  if request.method == "POST":
    next = request.GET.get('next')
    form = UserRegistrationForm(request.POST)
    if form.is_valid():
      user = form.save(commit=False)
      password = form.cleaned_data.get('password')
      # Get user type from form, default to 'practitioner' if not set
      user_type = form.cleaned_data.get('user_type') or 'practitioner'
      user.set_password(password)
      user.first_name = 'NONE'
      user.last_name = 'NONE'
      user.user_type = user_type  # Set user type (patient or practitioner)
      user.save()
      new_user = authenticate(email=user.email, password=password)
      login(request, new_user)
      if new_user.email_is_verified != True:
        new_user.send_email_verificaion()
      if next:
        return redirect(next)
      else:
        return redirect('/portal/')
  else:
    # Get user_type from URL parameter, default to 'practitioner' if not specified
    user_type = request.GET.get('user_type', 'practitioner')
    form = UserRegistrationForm(initial={'user_type': user_type})
  context = {
    'form': form
  }
  return render(request, 'registration/signup.html', context)

def verify_email(request):
    if request.method == "POST":
        if request.user.email_is_verified != True:
            request.user.send_email_verificaion
            return redirect('verify_email_done')
        else:
            return redirect('verify_email_complete')
    return render(request, 'registration/verify_email.html')

def verify_email_confirm(request, user_id_base64, token):
    try:
        user_id = force_str(urlsafe_base64_decode(user_id_base64))
        user = User.objects.get(pk=user_id)
    except(TypeError, ValueError, OverflowError, User.DoesNotExist):
        user = None
    if user is not None and account_activation_token.check_token(user, token):
        user.email_is_verified = True
        user.save()
        messages.success(request, 'Your email has been verified.')
        return redirect('verify_email_complete')
    else:
        messages.warning(request, 'The link is invalid.')
    return render(request, 'registration/verify_email_confirm.html')

def verify_email_done(request):
    return render(request, 'registration/verify_email_done.html')

def verify_email_complete(request):
    return render(request, 'registration/verify_email_complete.html')

def portal(request, path):
    return render(request, 'client/portal.html', {'foo': 'bar'})

def smart_launch(request):
    # TBD: Refactor/move this out of common
    SMART_CLIENT_ID = 'jhe1234'
    SMART_REDIRECT_URI = settings.SITE_URL+'/smart/callback'
    SMART_SCOPES = 'openid fhirUser launch launch/patient online_access patient/*.rs observation/*.rs'
    # https://build.fhir.org/ig/HL7/smart-app-launch/scopes-and-launch-context.html

    # 1) Initial launch URL is provided to EHR out of band
    # 2) User clicks to launch app which sends GET request to this view:
    # 3) /smart/launch?iss=https%3A%2F%2Flaunch.smarthealthit.org%2Fv%2Fr4%2Ffhir&launch=WzAsIiIsIiIsIkFVVE8
    logger.info(f'smart_launch request: {request.GET}')
    iss = request.GET.get("iss")
    launch = request.GET.get("launch")
    logger.info(f'iss: {iss}; launch: {launch}')
    # 4) We need to retrieve the public config from .well-known
    smart_config_response = requests.get(f'{iss}/.well-known/smart-configuration', headers={'Accept': 'application/json'})
    smart_config_data = smart_config_response.json()
    logger.info(f'smart_config: {smart_config_data}')
    # 5) Now we need to get the authroization token - we use config to construct the request
    #    See https://build.fhir.org/ig/HL7/smart-app-launch/app-launch.html#request-4

    auth_code_params = {
        'response_type': 'code', # fixed
        'client_id': SMART_CLIENT_ID,
        'redirect_uri': SMART_REDIRECT_URI,
        'launch': launch,
        'scope': SMART_SCOPES,
        'state': 'jheState1', # TBD map to user session - this is client-provided
        'aud': iss,
        'code_challenge': "AAc39YwnMSLwjXUVYSc1WY5tx45lSLj4eJ5CHyjY9Es",
        'code_challenge_method': 'S256'
    }

    # authorization_code = requests.get(smart_config_data.get('authorization_endpoint'), headers={'Accept': 'application/json'})
    smart_config_auth_endpoint = smart_config_data.get('authorization_endpoint')
    return redirect(f'{smart_config_auth_endpoint}?{urllib.parse.urlencode(auth_code_params)}')

def smart_callback(request):
    auth_code = request.GET.get("code")
    state = request.GET.get("state")

    SMART_REDIRECT_URI = settings.SITE_URL+'/smart/callback'

    smart_config_token_endpoint = "https://launch.smarthealthit.org/v/r4/auth/token" # from above smart_config_data.get('authorization_endpoint')

    token_params = {
        'grant_type': 'authorization_code', # fixed
        'code': auth_code,
        'redirect_uri': SMART_REDIRECT_URI,
        'code_verifier': "N0hHRVk2WDNCUUFPQTIwVDNZWEpFSjI4UElNV1pSTlpRUFBXNTEzU0QzRTMzRE85WDFWTzU2WU9ESw=="
    }

    token_response = requests.post(smart_config_token_endpoint, data=token_params, headers={'Accept': 'application/json'})
    token_data = token_response.json()

    logger.info(f'token_data: {token_data}')

    decoded_id_token = jwt.decode(str(token_data.get("id_token")), options={"verify_signature": False})

    logger.info(f'decoded_id_token: {decoded_id_token}')

    fhirUserId = decoded_id_token.get("fhirUser")

    user = User.objects.filter(identifier=fhirUserId).first()

    logger.info(f'Logging in user: {user}')

    # =========== Provisioning ===========
    # user = User(username='x')
    # set_unusable_password()
    # user.save()

    login(request, user, backend=settings.AUTHENTICATION_BACKENDS[0])

    logger.info(f'Save to server state, patient_id: {token_data.get("patient")}')

    return redirect('client-auth-login')
