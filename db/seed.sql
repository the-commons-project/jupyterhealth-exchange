-- USERS Passwords Jhe1234!

INSERT INTO public.core_jheuser (id, password, last_login, is_superuser, first_name, last_name, is_staff, is_active, date_joined, email, email_is_verified, identifier)
VALUES
    (10001, 'pbkdf2_sha256$870000$A1oqxU8FjILfREBJlD7OJj$drQcZtCdqHcthKOAlE+Ic8UxxpaEjtWro5lQQJTn7SI=', NULL, true, 'JHE', 'Super', true, true, NOW(), 'super@example.com', false, 'fhir-111'),
    (10002, 'pbkdf2_sha256$870000$A1oqxU8FjILfREBJlD7OJj$drQcZtCdqHcthKOAlE+Ic8UxxpaEjtWro5lQQJTn7SI=', NOW(), false, 'Anna', 'Pang', false, true, NOW(), 'anna@example.com', false, 'fhir-222'),
    (10003, 'pbkdf2_sha256$870000$A1oqxU8FjILfREBJlD7OJj$drQcZtCdqHcthKOAlE+Ic8UxxpaEjtWro5lQQJTn7SI=', NOW(), false, 'David', 'Dressler', false, true, NOW(), 'david@example.com', false, 'fhir-333'),
    (10004, 'pbkdf2_sha256$870000$A1oqxU8FjILfREBJlD7OJj$drQcZtCdqHcthKOAlE+Ic8UxxpaEjtWro5lQQJTn7SI=', NOW(), false, 'John', 'Chong', false, true, NOW(), 'john@example.com', false, 'fhir-444');
ALTER SEQUENCE core_jheuser_id_seq RESTART WITH 10005;


-- ORGANIZATIONS

INSERT INTO public.core_organization (id, name, part_of_id, type) VALUES
    (0, 'ROOT', NULL, 'root'),
    (20001, 'University of California San Francisco', 0, 'edu'),
    (20002, 'Department of Medicine', 20001, 'dept'),
    (20003, 'Cardiology', 20002, 'dept'),
    (20004, 'Cardio-Oncology and Immunology', 20003, 'dept'),
    (20005, 'Moslehi Lab', 20004, 'laboratory'),
    (20006, 'Olgin Lab', 20003, 'laboratory'),
    (20007, 'Department of Epidemiology & Biostatistics', 20001, 'root'),
    (20008, 'Kristine Yaffe Lab', 20007, 'laboratory'),
    (20009, 'Queens Community Clinic', 0, 'prov'),
    (20010, 'Providers', 20009, 'team'),
    (20011, 'UC Berkeley', 0, 'edu'),
    (20012, 'College of Computing, Data Science, and Society', 20011, 'edu'),
    (20013, 'Berkeley Institute for Data Science (BIDS)', 20012, 'edu');

ALTER SEQUENCE core_organization_id_seq RESTART WITH 20013;


-- STUDIES

ALTER SEQUENCE core_study_id_seq RESTART WITH 30001;


-- PATIENTS

ALTER SEQUENCE core_patient_id_seq RESTART WITH 40001;


-- SCOPES

INSERT INTO core_codeableconcept(id, coding_system, coding_code, text)
VALUES
    (50001, 'https://w3id.org/openmhealth', 'omh:blood-glucose:4.0', 'Blood glucose'),
    (50002, 'https://w3id.org/openmhealth', 'omh:blood-pressure:4.0', 'Blood pressure'),
    (50003, 'https://w3id.org/openmhealth', 'omh:body-temperature:3.0', 'Body temperature'),
    (50004, 'https://w3id.org/openmhealth', 'omh:oxygen-saturation:2.0', 'Oxygen saturation'),
    (50005, 'https://w3id.org/openmhealth', 'omh:heart-rate:2.0', 'Heart Rate');
ALTER SEQUENCE core_codeableconcept_id_seq RESTART WITH 50005;


-- OBSERVATIONS

ALTER SEQUENCE core_observation_id_seq RESTART WITH 60001;


-- DATA SOURCES

INSERT INTO core_datasource(id, name, type)
VALUES
    (70001, 'iHealth', 'personal_device'),
    (70002, 'Dexcom', 'personal_device');
ALTER SEQUENCE core_datasource_id_seq RESTART WITH 70003;
