"""JHE's side of the USCDI Patient Access client: the plugs the patient_access library needs
(directory, attrs, settings provider, delivery guard), JHE's own records for a pull, and the
patient session used by this client's views. The library never imports this package; JHE names
these in `settings.PATIENT_ACCESS`.
"""
