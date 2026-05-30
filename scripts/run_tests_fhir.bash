#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
pytest tests/test_model_methods.py::PatientFhirSearchTests::test_fhir_search_output_is_valid_fhir -v -s
