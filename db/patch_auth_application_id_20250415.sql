BEGIN;

UPDATE oauth2_provider_application
SET id = 1
WHERE id = 2;

UPDATE oauth2_provider_accesstoken
SET application_id = 1
WHERE application_id = 2;

UPDATE oauth2_provider_grant
SET application_id = 1
WHERE application_id = 2;

UPDATE oauth2_provider_idtoken
SET application_id = 1
WHERE application_id = 2;

UPDATE oauth2_provider_refreshtoken
SET application_id = 1
WHERE application_id = 2;

COMMIT;