INSERT INTO core_codeableconcept(coding_system, coding_code, text)
VALUES
    ('https://w3id.org/openmhealth', 'omh:oxygen-saturation:2.0', 'Oxygen saturation'),
    ('https://w3id.org/openmhealth', 'omh:respiratory-rate:2.0', 'Respiratory rate'),
    ('https://w3id.org/openmhealth', 'omh:rr-interval:1.0', 'RR Interval')
ON CONFLICT (coding_system, coding_code) DO NOTHING;