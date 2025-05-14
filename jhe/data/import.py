import csv
import json
from datetime import datetime

# DELETE FROM core_observation WHERE subject_patient_id IN (SELECT id FROM core_patient WHERE identifier LIKE '1636-%' or identifier LIKE '2133-%');

# INSERT INTO core_studypatientscopeconsent(scope_actions,consented,consented_time,scope_code_id,study_patient_id) SELECT 'rs',TRUE,NOW(),50001,id FROM core_studypatient WHERE patient_id IN (SELECT id FROM core_patient WHERE identifier LIKE '1636-%' OR identifier LIKE '2133-%');
# INSERT INTO core_studypatientscopeconsent(scope_actions,consented,consented_time,scope_code_id,study_patient_id) SELECT 'rs',TRUE,NOW(),50002,id FROM core_studypatient WHERE patient_id IN (SELECT id FROM core_patient WHERE identifier LIKE '1636-%' OR identifier LIKE '2133-%');
# INSERT INTO core_studypatientscopeconsent(scope_actions,consented,consented_time,scope_code_id,study_patient_id) SELECT 'rs',TRUE,NOW(),50005,id FROM core_studypatient WHERE patient_id IN (SELECT id FROM core_patient WHERE identifier LIKE '1636-%' OR identifier LIKE '2133-%');


mock_patients = [
  {
    "name_family": "Nguyen",
    "name_given": "Minh",
    "birth_date": "1984-07-11",
    "telecom_phone": "265-642-0143",
    "email": "minh.nguyen@example.com"
  },
  {
    "name_family": "Smith",
    "name_given": "Olivia",
    "birth_date": "1976-03-23",
    "telecom_phone": "187-554-0198",
    "email": "olivia.smith@example.com"
  },
  {
    "name_family": "Chen",
    "name_given": "Liang",
    "birth_date": "1948-11-30",
    "telecom_phone": "997-576-0102",
    "email": "liang.chen@example.com"
  },
  {
    "name_family": "Patel",
    "name_given": "Anika",
    "birth_date": "1989-01-17",
    "telecom_phone": "345-233-0170",
    "email": "anika.patel@example.com"
  },
  {
    "name_family": "Garcia",
    "name_given": "Carlos",
    "birth_date": "1955-05-04",
    "telecom_phone": "609-442-0186",
    "email": "carlos.garcia@example.com"
  },
  {
    "name_family": "Okafor",
    "name_given": "Chinelo",
    "birth_date": "1962-08-19",
    "telecom_phone": "435-287-0116",
    "email": "chinelo.okafor@example.com"
  },
  {
    "name_family": "Kowalski",
    "name_given": "Zofia",
    "birth_date": "1945-02-14",
    "telecom_phone": "399-765-0124",
    "email": "zofia.kowalski@example.com"
  },
  {
    "name_family": "Tanaka",
    "name_given": "Hiroshi",
    "birth_date": "1958-10-01",
    "telecom_phone": "298-443-0131",
    "email": "hiroshi.tanaka@example.com"
  },
  {
    "name_family": "Abdullah",
    "name_given": "Layla",
    "birth_date": "1973-12-25",
    "telecom_phone": "198-619-0149",
    "email": "layla.abdullah@example.com"
  },
  {
    "name_family": "Dubois",
    "name_given": "Émile",
    "birth_date": "1981-06-03",
    "telecom_phone": "400-870-0162",
    "email": "émile.dubois@example.com"
  },
  {
    "name_family": "Singh",
    "name_given": "Raj",
    "birth_date": "1992-09-12",
    "telecom_phone": "398-112-0181",
    "email": "raj.singh@example.com"
  },
  {
    "name_family": "Martinez",
    "name_given": "Sofia",
    "birth_date": "1967-07-27",
    "telecom_phone": "229-998-0108",
    "email": "sofia.martinez@example.com"
  },
  {
    "name_family": "Kim",
    "name_given": "Jisoo",
    "birth_date": "1950-04-20",
    "telecom_phone": "988-889-0157",
    "email": "jisoo.kim@example.com"
  },
  {
    "name_family": "Ivanov",
    "name_given": "Dmitri",
    "birth_date": "1983-02-05",
    "telecom_phone": "799-443-0129",
    "email": "dmitri.ivanov@example.com"
  },
  {
    "name_family": "Mbatha",
    "name_given": "Sipho",
    "birth_date": "1979-11-08",
    "telecom_phone": "762-112-0140",
    "email": "sipho.mbatha@example.com"
  },
  {
    "name_family": "Rossi",
    "name_given": "Giulia",
    "birth_date": "1960-05-30",
    "telecom_phone": "772-981-0169",
    "email": "giulia.rossi@example.com"
  },
  {
    "name_family": "Hernandez",
    "name_given": "Luis",
    "birth_date": "1952-03-14",
    "telecom_phone": "118-112-0194",
    "email": "luis.hernandez@example.com"
  },
  {
    "name_family": "Yilmaz",
    "name_given": "Aylin",
    "birth_date": "1972-01-01",
    "telecom_phone": "388-887-0175",
    "email": "aylin.yilmaz@example.com"
  },
  {
    "name_family": "Andersson",
    "name_given": "Lars",
    "birth_date": "1988-10-10",
    "telecom_phone": "334-874-0111",
    "email": "lars.andersson@example.com"
  },
  {
    "name_family": "Ali",
    "name_given": "Zara",
    "birth_date": "1947-06-06",
    "telecom_phone": "202-555-0188",
    "email": "zara.ali@example.com"
  }
]

mhealth_glucose = {
  "body": {
    "blood_glucose": {
      "unit": "MGDL",
      "value": None
    },
    "effective_time_frame": {
      "date_time": None
    },
    "temporal_relationship_to_meal": "unknown"
  },
  "header": {
    "modality": "self-reported",
    "schema_id": {
      "name": "blood-glucose",
      "version": "3.1",
      "namespace": "omh"
    },
    "creation_date_time": None,
    "external_datasheets": [
      {
        "datasheet_type": "manufacturer",
        "datasheet_reference": "Dexcom"
      }
    ],
    "source_creation_date_time": None
  }
}

patients = {}
placeholder = 'pbkdf2_sha256$870000$EOy0Dhs6tfzGwHQXN9Auzp$NUiNL2psJpZpjDeWgDyd80gDFy8WIfYBmw3Jc/IJBoI='
organization_id = 20013
codeable_concept_id = 50001
status = "final"
data_source_id = 70002

with open('iglu_example_data_hall_patch.sql', 'w') as outfile:
    outfile.write("BEGIN;")
    with open('iglu_example_data_hall.csv', newline='') as csvfile:
        reader = csv.reader(csvfile)
        row_count = 0
        next(reader)  # Skip the header row
        for row in reader:
            row_count += 1
            parsed_date = datetime.strptime("2024"+row[2][4:], "%Y-%m-%d %H:%M:%S")
            parsed_date_str = parsed_date.strftime("%Y-%m-%dT%H:%M:%SZ")
            
            if row[1] not in patients:
                patient = mock_patients[len(patients)]
                patients[row[1]] = patient
                # print(patient)
                #outfile.write("\n\n")
                #outfile.write(f"INSERT INTO core_jheuser VALUES (DEFAULT,'{placeholder}',NOW(),FALSE,'{patient['name_given']}','{patient['name_family']}',FALSE,TRUE,NOW(),'{patient['email']}',TRUE, '{row[1]}');")
                #outfile.write("\n")
                #outfile.write(f"INSERT INTO core_patient(jhe_user_id, identifier, name_family, name_given, birth_date, telecom_phone, last_updated, organization_id) SELECT id, '{row[1]}', '{patient['name_family']}', '{patient['name_given']}', '{patient['birth_date']}', '{patient['telecom_phone']}', NOW(), {organization_id} FROM core_jheuser WHERE email='{patient['email']}';")
                #outfile.write("\n\n")
            
            mhealth_glucose["body"]["blood_glucose"]["value"] = int(row[3])
            mhealth_glucose["body"]["effective_time_frame"]["date_time"] = parsed_date_str
            mhealth_glucose["header"]["creation_date_time"] = parsed_date_str
            mhealth_glucose["header"]["source_creation_date_time"] = parsed_date_str

            # print(mhealth_glucose)
            outfile.write(f"INSERT INTO core_observation(subject_patient_id, value_attachment_data, last_updated, codeable_concept_id, status, data_source_id) SELECT id, '{json.dumps(mhealth_glucose)}', NOW(), '{codeable_concept_id}', '{status}', '{data_source_id}' FROM core_patient WHERE identifier='{row[1]}';")
            outfile.write("\n")

    print(f"Patient count: {len(patients)}")
    print(f"Record count: {row_count}")
    outfile.write("\nCOMMIT;")